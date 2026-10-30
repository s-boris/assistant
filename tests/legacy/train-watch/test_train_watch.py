"""Offline tests for ~/bin/train-watch (TRAIN_WATCH_BIN=/path/to/copy tests a copy).

A loopback fake of the transport API (shape of transport.opendata.ch: /connections by from, to, date, time, limit;
times with +0100 offsets; delay, prognosis.departure/arrival/platform) serves a synthetic timetable that each test
sets; the same timetable in timetable.search.ch's shape (route.json by from, to, date dd.mm.yyyy, time, num, pre;
wall times without offset, dep_delay/arr_delay "+N", "X" = cancelled, track "4!" = changed) is the fallback; every
request is recorded. SearchCh reruns every TW test with opendata down (an {"errors": ...} payload with 200). http(s)_proxy point at the fake too, so nothing leaves the box even if the seam
breaks. Fictional stops, a synthetic profile and temporary state/inbox dirs only.
Covers: cron fast path (no legs, outside the window, after departure), on time, delay + buckets + dedup,
platform change, cancelled flag, missing connection (two runs / close to departure), replacement bus (EV, bus where
the plan had trains), timetable change, transfer at risk, alternative choice and "no faster alternative", SBB link,
arrival-based match, API errors without tracebacks, calendar sync (link stations, title fallback, lines, filters,
removed events, manual legs kept), add/list/rm/usage errors, pruning, status, dry run, note file naming.
"""
import datetime as dt
import glob
import http.server
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
import zoneinfo

BIN = os.environ.get('TRAIN_WATCH_BIN') or os.path.expanduser('~/bin/train-watch')
DAY = '2026-11-14'
A, B, C = 'Alphadorf', 'Betastadt HB', 'Gammawil'
CET = dt.timezone(dt.timedelta(hours=1))
ZH = zoneinfo.ZoneInfo('Europe/Zurich')


def t(hhmm, day=DAY):
    return f'{day}T{hhmm}:00+0100'


def plus(hhmm, m):
    h, mm = map(int, hhmm.split(':'))
    x = dt.datetime(2026, 11, 14, h, mm) + dt.timedelta(minutes=m)
    return x.strftime('%H:%M')


def ride(cat, num, frm, dep, to, arr, pl='1', dd=None, ad=None, new_pl=None, cancelled=False, name='000001'):
    d = {'departure': t(dep), 'delay': dd, 'platform': pl, 'station': {'name': frm},
         'prognosis': {'departure': t(plus(dep, dd)) if dd else None, 'platform': new_pl, 'arrival': None}}
    a = {'arrival': t(arr), 'delay': ad, 'platform': None, 'station': {'name': to},
         'prognosis': {'arrival': t(plus(arr, ad)) if ad else None, 'platform': None, 'departure': None}}
    s = {'walk': None, 'journey': {'category': cat, 'number': num, 'to': to, 'name': name, 'operator': 'XB'},
         'departure': d, 'arrival': a}
    if cancelled:
        s['journey']['cancelled'] = True
    return s


def conn(*rides):
    f, l = rides[0], rides[-1]
    return {'from': {'departure': f['departure']['departure'], 'delay': f['departure']['delay'],
                     'station': {'name': f['departure']['station']['name']},
                     'prognosis': dict(f['departure']['prognosis'])},
            'to': {'arrival': l['arrival']['arrival'], 'delay': l['arrival']['delay'],
                   'station': {'name': l['arrival']['station']['name']},
                   'prognosis': dict(l['arrival']['prognosis'])},
            'duration': '00d00:52:00', 'transfers': len(rides) - 1, 'sections': list(rides)}


def s5(dep, arr, **kw):
    return conn(ride('S', '5', A, dep, B, arr, **kw))


def base(**over):
    """Half-hourly S5 Alphadorf -> Betastadt HB; over = {dep: connection} replaces one."""
    rows = {d: s5(d, a) for d, a in [('15:01', '15:53'), ('15:31', '16:23'), ('16:01', '16:53'), ('16:31', '17:23'),
                                     ('17:01', '17:53'), ('17:31', '18:23')]}
    rows.update(over)
    return [c for c in rows.values() if c is not None]


def wall(s):
    """'...T16:31:00+0100' -> search.ch's '2026-11-14 16:31:00' (Zurich wall time, no offset)."""
    if not s:
        return None
    s = s[:-2] + ':' + s[-2:] if s[-5] in '+-' and s[-3] != ':' else s
    return dt.datetime.fromisoformat(s).astimezone(ZH).strftime('%Y-%m-%d %H:%M:%S')


def searchch(c):
    """One opendata-shaped connection -> search.ch route.json shape."""
    legs = []
    for s in c['sections']:
        j, d, a = s['journey'], s['departure'], s['arrival']
        if not j:                                       # a walk (from the door)
            legs.append({'departure': wall(d['departure']), 'name': d['station']['name'], 'type': 'walk',
                         'exit': {'arrival': wall(a['arrival']), 'name': a['station']['name']}})
            continue
        cat, num = j['category'], j['number']
        track = (d['prognosis'].get('platform') + '!') if d['prognosis'].get('platform') else d['platform']
        kind = {'B': 'bus', 'EV': 'bus', 'T': 'tram'}.get(cat, 'strain' if cat == 'S' else 'express_train')
        legs.append({'departure': wall(d['departure']), 'name': d['station']['name'], 'type': kind,
                     'line': f'{cat}{num}' if cat in ('S', 'IC', 'IR', 'RE') else num, '*G': cat, '*L': num,
                     'terminal': j['to'], 'operator': j['operator'], 'track': track,
                     'dep_delay': 'X' if j.get('cancelled') else (f"+{d['delay']}" if d['delay'] else '+0'),
                     'exit': {'arrival': wall(a['arrival']), 'name': a['station']['name'], 'track': '',
                              'arr_delay': f"+{a['delay']}" if a['delay'] else '+0'}})
    last = c['sections'][-1]['arrival']
    legs.append({'arrival': wall(last['arrival']), 'name': last['station']['name']})
    return {'from': c['from']['station']['name'], 'departure': wall(c['from']['departure']),
            'dep_delay': f"+{c['from']['delay'] or 0}", 'to': c['to']['station']['name'],
            'arrival': wall(c['to']['arrival']), 'arr_delay': f"+{c['to']['delay'] or 0}", 'legs': legs}


class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        srv = self.server
        srv.requests.append((u.path, q))
        if srv.mode == 'error':
            return self.send(500, {'errors': [{'message': 'backend down'}]})
        if srv.mode == 'junk':
            return self.send(200, None, raw=b'<html>nope</html>')
        if u.path.endswith('/connections') and srv.mode == 'down':      # opendata's router outage: 200 + errors
            return self.send(200, {'errors': [{'message': 'The router is not reachable'}]})
        if u.path.endswith('/connections') and srv.mode == 'down503':
            return self.send(503, {'errors': [{'message': 'The router is not reachable'}]})
        if u.path.endswith('/route.json'):
            dd, mm, yy = q['date'].split('.')
            when = f"{yy}-{mm}-{dd} {q['time']}:00"
            rows = sorted((searchch(c) for c in srv.timetable if c['sections'][0]['departure']['station']['name']
                           == q.get('from') and c['to']['station']['name'] == q.get('to')),
                          key=lambda c: c['departure'])
            i = next((n for n, c in enumerate(rows) if c['departure'] >= when), len(rows))
            rows = rows[max(0, i - int(q.get('pre') or 0)):i + int(q.get('num') or 4)]
            return self.send(200, {'count': len(rows), 'connections': rows})
        if u.path.endswith('/connections'):
            when = f"{q.get('date')}T{q.get('time')}:00+0100"
            rows = [c for c in srv.timetable if c['sections'][0]['departure']['station']['name'] == q.get('from')
                    and c['to']['station']['name'] == q.get('to') and c['from']['departure'] >= when]
            rows.sort(key=lambda c: c['from']['departure'])
            return self.send(200, {'connections': rows[:int(q.get('limit') or 4)]})
        return self.send(404, {'errors': [{'message': 'no such endpoint'}]})

    def send(self, code, obj, raw=None):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)


class TW(unittest.TestCase):
    MODE = 'ok'                                         # SearchCh: 'down' (opendata errors, search.ch answers)

    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fake)
        cls.srv.requests, cls.srv.timetable, cls.srv.mode = [], [], 'ok'
        cls.url = f'http://127.0.0.1:{cls.srv.server_port}/v1'
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.dir, self.inbox = os.path.join(d, 'state'), os.path.join(d, 'inbox')
        prof = os.path.join(d, 'profile.json')
        with open(prof, 'w') as f:
            json.dump({'locale': {'tz': 'Europe/Zurich'}}, f)
        self.env = dict(os.environ, TRAIN_WATCH_API=self.url, TRAIN_WATCH_DIR=self.dir, TRAIN_WATCH_INBOX=self.inbox,
                        TRAIN_WATCH_FALLBACK_API=self.url.replace('/v1', '/sc'),
                        ASSISTANT_PROFILE=prof, http_proxy=self.url, https_proxy=self.url,
                        HTTP_PROXY=self.url, HTTPS_PROXY=self.url, no_proxy='', NO_PROXY='',
                        HOME_PLACE_GEO='off', HOME_PLACE_CACHE=os.path.join(d, 'home-place.json'))
        self.env.pop('TRAIN_WATCH_NOW', None)
        self.env.pop('ROUTE_API', None)
        self.srv.requests.clear()
        self.srv.timetable, self.srv.mode = base(), self.MODE

    def tearDown(self):
        self.tmp.cleanup()

    def run_tw(self, *args, now=None, stdin=None):
        env = dict(self.env)
        if now:
            env['TRAIN_WATCH_NOW'] = f'{DAY}T{now}:00+01:00'
        return subprocess.run([sys.executable, BIN, *args], env=env, capture_output=True, text=True, input=stdin,
                              timeout=60)

    def add_leg(self, dep='16:31', arr='17:23', lines=('S5',), src=A, dst=B, now='09:00'):
        args = ['add', src, dst, f'{DAY} {dep}'] + (['--arr', arr] if arr else [])
        for x in lines:
            args += ['--line', x]
        r = self.run_tw(*args, now=now)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.split()[1].rstrip(':')

    def lookups(self):
        """(from, to, date, time) of every timetable lookup, whichever source got it (dates as YYYY-MM-DD)."""
        out = []
        for p, q in self.srv.requests:
            if p.endswith('/route.json'):
                out.append((q['from'], q['to'], '-'.join(reversed(q['date'].split('.'))), q['time']))
            elif p.endswith('/connections') and self.MODE == 'ok':
                out.append((q['from'], q['to'], q['date'], q['time']))
        return out

    def notes(self):
        return sorted(glob.glob(os.path.join(self.inbox, 'train-watch-*.md')))

    def note_text(self, i=-1):
        with open(self.notes()[i]) as f:
            return f.read()

    def check(self, now, *extra):
        r = self.run_tw('check', '--cron', *extra, now=now)
        self.assertNotIn('Traceback', r.stderr)
        return r

    # ------------------------------------------------------------------ cron fast path and window
    def test_help(self):
        r = self.run_tw('--help')
        self.assertEqual(r.returncode, 0)
        for w in ('sync', 'add', 'check', 'status'):
            self.assertIn(w, r.stdout)

    def test_no_legs_file_is_silent(self):
        r = self.check('15:00')
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '', ''))
        self.assertEqual(self.srv.requests, [])
        self.assertFalse(os.path.exists(self.inbox))

    def test_outside_window_no_request(self):
        self.add_leg()
        for now in ('13:30', '16:32'):            # window 13:31..16:31
            r = self.check(now)
            self.assertEqual((r.returncode, r.stdout), (0, ''))
        self.assertEqual(self.srv.requests, [])

    def test_on_time_is_silent(self):
        self.add_leg()
        r = self.check('13:31')
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '', ''))
        self.assertEqual(self.notes(), [])
        self.assertEqual(self.lookups()[0], (A, B, DAY, '16:31'))

    # ------------------------------------------------------------------ problems
    def test_delay_note_dedup_and_buckets(self):
        self.add_leg()
        self.srv.timetable = base(**{'16:31': s5('16:31', '17:23', dd=7, ad=7)})
        self.check('15:00')
        self.assertEqual(len(self.notes()), 1)
        txt = self.note_text()
        self.assertIn('delayed: leaves 16:38 (+7 min), arrives 17:30 (+7 min)', txt)
        self.assertIn('Suggested alternative: 16:01 S5 → 16:53', txt)
        epoch = int(dt.datetime(2026, 11, 14, 16, 1, tzinfo=CET).timestamp())
        self.assertIn(f'https://app.sbbmobile.ch/timetable?from={A}&to=Betastadt%20HB&time={epoch}', txt)
        self.assertIn(f'{A} → {B}', txt)
        self.check('15:10')                                   # same problem: silent
        self.assertEqual(len(self.notes()), 1)
        self.srv.timetable = base(**{'16:31': s5('16:31', '17:23', dd=16, ad=16)})
        self.check('15:20')                                   # worse bucket: notes again
        self.assertEqual(len(self.notes()), 2)
        self.assertIn('+16 min', self.note_text())
        self.srv.timetable = base(**{'16:31': s5('16:31', '17:23', dd=8, ad=8)})
        self.check('15:30')                                   # back to a lower bucket: silent
        self.assertEqual(len(self.notes()), 2)

    def test_small_delay_silent(self):
        self.add_leg()
        self.srv.timetable = base(**{'16:31': s5('16:31', '17:23', dd=4, ad=3)})
        self.check('15:00')
        self.assertEqual(self.notes(), [])

    def test_no_faster_alternative(self):
        self.add_leg()
        self.srv.timetable = base(**{'16:31': s5('16:31', '17:23', dd=6, ad=6),
                                     '16:01': s5('16:01', '16:53', dd=45, ad=45)})
        self.check('15:00')
        self.assertIn('No faster alternative: the planned connection still arrives first (17:29)', self.note_text())

    def test_platform_change(self):
        self.add_leg()
        self.srv.timetable = base(**{'16:31': s5('16:31', '17:23', new_pl='4')})
        self.check('15:00')
        self.assertIn(f'platform change at {A}: S5 now leaves from pl 4 (was 1)', self.note_text())
        self.check('15:10')
        self.assertEqual(len(self.notes()), 1)

    def test_cancelled_flag_then_missing_is_same_problem(self):
        self.add_leg()
        self.srv.timetable = base(**{'16:31': s5('16:31', '17:23', cancelled=True)})
        self.check('15:00')
        txt = self.note_text()
        self.assertIn(f'cancelled: S5 16:31 from {A}', txt)
        self.assertIn('Suggested alternative: 16:01 S5 → 16:53', txt)
        self.srv.timetable = base(**{'16:31': None})
        self.check('15:10')
        self.check('15:20')
        self.assertEqual(len(self.notes()), 1)

    def test_missing_needs_two_runs(self):
        self.add_leg()
        self.srv.timetable = base(**{'16:31': None})
        self.check('15:00')
        self.assertEqual(self.notes(), [])
        self.check('15:10')
        self.assertEqual(len(self.notes()), 1)
        self.assertIn('gone from the live timetable', self.note_text())

    def test_missing_close_to_departure_notes_at_once(self):
        self.add_leg()
        self.srv.timetable = base(**{'16:31': None})
        self.check('16:10')
        self.assertEqual(len(self.notes()), 1)
        self.assertIn('Suggested alternative: 17:01 S5 → 17:53', self.note_text())

    def test_missing_resets_when_back(self):
        self.add_leg()
        self.srv.timetable = base(**{'16:31': None})
        self.check('15:00')
        self.srv.timetable = base()
        self.check('15:10')
        self.srv.timetable = base(**{'16:31': None})
        self.check('15:20')
        self.assertEqual(self.notes(), [])

    def test_replacement_bus_ev(self):
        self.add_leg()
        self.srv.timetable = base(**{'16:31': conn(ride('EV', '5', A, '16:31', B, '17:23'))})
        self.check('15:00')
        self.assertIn(f'replacement bus: EV 5 {A} 16:31 → {B} 17:23 instead of the train', self.note_text())

    def test_bus_where_plan_had_trains(self):
        self.add_leg()
        self.srv.timetable = base(**{'16:31': conn(ride('B', '805', A, '16:31', B, '17:23'))})
        self.check('15:00')
        self.assertIn('replacement bus: Bus 805', self.note_text())

    def test_planned_bus_is_fine(self):
        self.add_leg(lines=('Bus 7', 'S5'))
        self.srv.timetable = base(**{'16:31': conn(ride('B', '7', A, '16:31', C, '16:40'),
                                                   ride('S', '5', C, '16:45', B, '17:23'))})
        self.check('15:00')
        self.assertEqual(self.notes(), [])

    def test_timetable_change(self):
        self.add_leg()
        self.srv.timetable = base(**{'16:31': s5('16:31', '17:31')})
        self.check('15:00')
        self.assertIn('timetable change: now scheduled to arrive 17:31 instead of 17:23', self.note_text())

    def test_transfer_at_risk(self):
        self.add_leg(lines=('S5', 'S7'))
        self.srv.timetable = base(**{'16:31': conn(ride('S', '5', A, '16:31', C, '16:50', ad=4),
                                                   ride('S', '7', C, '16:54', B, '17:23', pl='3'))})
        self.check('15:00')
        txt = self.note_text()
        self.assertIn(f'change at {C} at risk: S5 arrives 16:54, S7 leaves 16:54', txt)
        self.assertNotIn('delayed', txt)

    def test_arrival_match_for_door_start(self):
        self.add_leg(dep='16:20', arr='17:23')                # event starts at the door; the train leaves 16:31
        self.check('15:00')
        self.assertEqual(self.notes(), [])
        self.srv.timetable = base(**{'16:31': s5('16:31', '17:23', dd=9, ad=9)})
        self.check('15:10')
        self.assertIn('+9 min', self.note_text())

    def test_same_train_another_way_to_it(self):
        """The router takes another bus to the planned train and arrives 3 min off: the same trip, not gone."""
        lid = self.add_leg(dep='16:26', arr='17:26', lines=('Bus 9', 'S5'), src=C)
        c = conn(ride('B', '7', C, '16:28', A, '16:30'), ride('S', '5', A, '16:31', B, '17:23', dd=6, ad=6))
        self.srv.timetable = base() + [c]
        self.check('16:10')                                  # under 25 min: a miss would note at once
        n = self.note_text()
        self.assertIn('+6 min', n)
        self.assertNotIn('gone from the live timetable', n)
        self.srv.timetable = base() + [conn(ride('B', '7', C, '16:28', A, '16:30'), ride('IR', '9', A, '16:31', B,
                                                                                       '17:23'))]
        j = json.loads(self.run_tw('status', lid, '--json', now='16:10').stdout)
        self.assertIn('cancelled', [p['key'] for p in j['problems']])   # no planned train in it: gone

    # ------------------------------------------------------------------ errors, dry run, status
    def test_api_error_exit_1_no_traceback(self):
        self.add_leg()
        for mode in ('error', 'junk'):
            self.srv.mode = mode
            r = self.check('15:00')
            self.assertEqual(r.returncode, 1)
            self.assertIn('transport API', r.stderr)
        self.assertEqual(self.notes(), [])

    def test_api_unreachable(self):
        self.add_leg()
        self.env.update(TRAIN_WATCH_API='http://127.0.0.1:9/v1', TRAIN_WATCH_FALLBACK_API='http://127.0.0.1:9/sc',
                        no_proxy='127.0.0.1', NO_PROXY='127.0.0.1')
        r = self.check('15:00')
        self.assertEqual(r.returncode, 1)
        self.assertIn('fallback search.ch: transport API unreachable', r.stderr)

    def test_dry_run_writes_nothing(self):
        lid = self.add_leg()
        self.srv.timetable = base(**{'16:31': s5('16:31', '17:23', dd=7, ad=7)})
        for _ in range(2):
            r = self.run_tw('check', '--dry-run', now='15:00')
            self.assertIn('delayed', r.stdout)
        self.assertEqual(self.notes(), [])
        with open(os.path.join(self.dir, 'state.json')) as f:
            self.assertEqual(json.load(f)['legs'].get(lid, {}).get('noted', []), [])

    def test_status_live_no_state(self):
        lid = self.add_leg()
        r = self.run_tw('status', lid, now='09:00')            # outside the window: still answers
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Status: fine', r.stdout)
        self.assertIn('Live: 16:31 S5 → 17:23, pl 1, direct', r.stdout)
        self.srv.timetable = base(**{'16:31': None})
        r = self.run_tw('status', lid, now='09:00')
        self.assertIn('gone from the live timetable', r.stdout)
        self.assertIn('Suggested alternative', r.stdout)
        self.assertEqual(self.notes(), [])
        with open(os.path.join(self.dir, 'state.json')) as f:
            self.assertEqual(json.load(f)['legs'], {})

    def test_status_json(self):
        lid = self.add_leg()
        r = self.run_tw('status', lid, '--json', now='09:00')
        self.assertEqual(r.returncode, 0, r.stderr)
        j = json.loads(r.stdout)
        self.assertEqual((j['id'], j['fine'], j['problems'], j['alt']), (lid, True, [], None))
        self.assertEqual({k: j['live'][k] for k in ('dep', 'arr', 'delay', 'cancelled', 'platform', 'changes')},
                         {'dep': '16:31', 'arr': '17:23', 'delay': 0, 'cancelled': False, 'platform': '1', 'changes': 0})
        self.assertEqual((j['from'], j['to'], j['lines']), (A, B, ['S5']))
        self.srv.timetable = base(**{'16:31': s5('16:31', '17:23', dd=7, ad=7)})
        j = json.loads(self.run_tw('status', lid, '--json', now='16:00').stdout)
        self.assertFalse(j['fine'])
        self.assertEqual([p['key'] for p in j['problems']], ['delay5'])
        self.assertEqual(j['live']['delay'], 7)
        self.srv.timetable = base(**{'16:31': None})
        j = json.loads(self.run_tw('status', lid, '--json', now='09:00').stdout)
        self.assertEqual(([p['key'] for p in j['problems']], j['live']), (['cancelled'], None))
        self.assertTrue(j['alt'] and j['alt']['dep'] and j['alt']['link'].startswith('https://app.sbbmobile.ch/'))
        self.assertEqual(self.notes(), [])
        r = self.run_tw('status', 'nope', '--json', now='09:00')
        self.assertEqual(r.returncode, 2)

    def test_note_file_name(self):
        lid = self.add_leg()
        self.srv.timetable = base(**{'16:31': s5('16:31', '17:23', dd=5, ad=5)})
        self.check('15:00')
        epoch = int(dt.datetime(2026, 11, 14, 15, 0, tzinfo=CET).timestamp())
        self.assertEqual([os.path.basename(p) for p in self.notes()], [f'train-watch-{lid}-{epoch}.md'])
        self.assertEqual(sorted(os.listdir(self.inbox)), [f'train-watch-{lid}-{epoch}.md'])
        self.assertIn(f'Leg: {lid} (manual)', self.note_text())

    # ------------------------------------------------------------------ legs: add, list, rm, sync
    def test_add_list_rm_and_usage(self):
        lid = self.add_leg(lines=('S5',))
        r = self.run_tw('list', now='09:00')
        self.assertIn(f'{lid}  Sat 14.11 16:31 → 17:23  {A} → {B}  [S5]  manual  (watch from Sat 14.11 13:31)', r.stdout)
        r = self.run_tw('list', '--json', now='09:00')
        self.assertEqual(json.loads(r.stdout)[0]['dep'], '2026-11-14T16:31:00+01:00')
        for bad in (['add', A, B, '14.11.2026 16:31'], ['add', A, A, f'{DAY} 16:31'],
                    ['add', A, B, f'{DAY} 16:31', '--arr', '25:00'], ['add', A, B, '2026-11-13 16:31'],
                    ['rm', 'mnope'], ['bogus']):
            r = self.run_tw(*bad, now='09:00')
            self.assertEqual(r.returncode, 2, bad)
            self.assertNotIn('Traceback', r.stderr)
        r = self.run_tw('rm', lid, now='09:00')
        self.assertEqual(r.returncode, 0)
        self.assertIn('no legs ahead', self.run_tw('list', now='09:00').stdout)

    def test_prune_old_legs(self):
        self.add_leg(now='09:00')
        r = self.run_tw('add', A, C, '2026-11-20 08:00', now='09:00')
        self.assertEqual(r.returncode, 0)
        env = dict(self.env, TRAIN_WATCH_NOW='2026-11-16T09:00:00+01:00')
        subprocess.run([sys.executable, BIN, 'add', B, C, '2026-11-21 08:00'], env=env, capture_output=True)
        with open(os.path.join(self.dir, 'legs.json')) as f:
            deps = [g['dep'][:10] for g in json.load(f)['legs']]
        self.assertEqual(deps, ['2026-11-20', '2026-11-21'])

    def sync_events(self, events, now='09:00'):
        p = os.path.join(self.tmp.name, 'cal.json')
        with open(p, 'w') as f:
            json.dump({'events': events}, f)
        return self.run_tw('sync', p, now=now)

    def test_sync_calendar_events(self):
        link = ('https://app.sbbmobile.ch/timetable?from=Alphadorf&to=Betastadt%20HB&time='
                + str(int(dt.datetime(2026, 11, 14, 16, 31, tzinfo=CET).timestamp())))
        ev = [
            {'id': 'e1', 'status': 'confirmed', 'summary': '\U0001F686 S5 Alphadorf → Betastadt HB',
             'description': f'16:31 S5 Alphadorf pl 1 › 17:23 Betastadt HB · direct\nFallback: S5 17:01\nSBB: {link}',
             'start': {'dateTime': '2026-11-14T16:31:00+01:00'}, 'end': {'dateTime': '2026-11-14T17:23:00+01:00'}},
            {'id': 'e2', 'summary': '\U0001F686 Gammawil → Alphadorf (after the show)',
             'description': '22:10 Bus 7 › IR12 Delta › 23:01 Alphadorf · 1 change',
             'start': {'dateTime': '2026-11-14T22:10:00+01:00'}, 'end': {'dateTime': '2026-11-14T23:01:00+01:00'}},
            {'id': 'e3', 'summary': 'Show night', 'start': {'dateTime': '2026-11-14T20:00:00+01:00'},
             'end': {'dateTime': '2026-11-15T02:00:00+01:00'}},
            {'id': 'e4', 'status': 'cancelled', 'summary': '\U0001F686 S5 Alphadorf → Betastadt HB',
             'start': {'dateTime': '2026-11-15T16:31:00+01:00'}, 'end': {'dateTime': '2026-11-15T17:23:00+01:00'}},
            {'id': 'e5', 'summary': '\U0001F686 trip day', 'start': {'date': '2026-11-16'}, 'end': {'date': '2026-11-17'}},
            {'id': 'e6', 'summary': '\U0001F686 S5 Alphadorf → Betastadt HB',
             'start': {'dateTime': '2026-11-13T16:31:00+01:00'}, 'end': {'dateTime': '2026-11-13T17:23:00+01:00'}},
        ]
        self.add_leg(src=B, dst=C, dep='12:00', arr=None, lines=())
        r = self.sync_events(ev)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('synced 2 calendar legs (0 removed), kept 1 added by hand', r.stdout)
        with open(os.path.join(self.dir, 'legs.json')) as f:
            legs = {g['event_id']: g for g in json.load(f)['legs'] if g['source'] == 'calendar'}
        self.assertEqual(sorted(legs), ['e1', 'e2'])
        self.assertEqual((legs['e1']['from'], legs['e1']['to'], legs['e1']['lines']), (A, B, ['S5']))
        self.assertEqual(legs['e1']['arr'], '2026-11-14T17:23:00+01:00')
        self.assertEqual((legs['e2']['from'], legs['e2']['to'], legs['e2']['lines']), (C, A, ['Bus 7', 'IR12']))
        r = self.sync_events(ev[:1])
        self.assertIn('synced 1 calendar legs (1 removed), kept 1 added by hand', r.stdout)
        r = self.run_tw('sync', '-', now='09:00', stdin=json.dumps([{'events': ev[1:2]}]))
        self.assertIn('synced 1 calendar legs (1 removed)', r.stdout)
        r = self.run_tw('sync', os.path.join(self.tmp.name, 'none.json'), now='09:00')
        self.assertEqual(r.returncode, 2)

    LEG = {'id': 'e1', 'summary': '\U0001F686 S5 Alphadorf → Betastadt HB',
           'start': {'dateTime': '2026-11-14T16:31:00+01:00'}, 'end': {'dateTime': '2026-11-14T17:23:00+01:00'}}
    OTHER = {'id': 'e9', 'summary': 'Dentist', 'start': {'dateTime': '2026-11-14T10:00:00+01:00'},
             'end': {'dateTime': '2026-11-14T11:00:00+01:00'}}

    def legs_file(self):
        with open(os.path.join(self.dir, 'legs.json')) as f:
            return f.read()

    def test_sync_items_key(self):
        # the Google API shape: events under "items", the page's own "summary" is the calendar's name
        page = {'kind': 'calendar#events', 'summary': 'Primary', 'items': [self.LEG]}
        r = self.run_tw('sync', '-', now='09:00', stdin=json.dumps(page))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('synced 1 calendar legs (0 removed)', r.stdout)
        r = self.run_tw('sync', '-', now='09:00', stdin=json.dumps({'kind': 'calendar#events', 'summary': 'Primary'}))
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('no list_events result', r.stderr)

    def test_sync_empty_source_keeps_legs(self):
        self.assertEqual(self.run_tw('sync', '-', now='09:00', stdin=json.dumps({'events': [self.LEG]})).returncode, 0)
        before = self.legs_file()
        for data in ({'events': []}, {'items': []}, []):
            r = self.run_tw('sync', '-', now='09:00', stdin=json.dumps(data))
            self.assertEqual(r.returncode, 2, (data, r.stdout))
            self.assertIn('keeping the 1 upcoming calendar legs', r.stderr)
            self.assertEqual(self.legs_file(), before)
        r = self.run_tw('sync', '-', now='09:00', stdin=json.dumps({'events': [self.OTHER]}))  # no trains: a real removal
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('synced 0 calendar legs (1 removed)', r.stdout)
        self.run_tw('sync', '-', now='09:00', stdin=json.dumps({'events': [self.LEG]}))
        r = self.run_tw('sync', '-', '--allow-empty', now='09:00', stdin=json.dumps({'events': []}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('(1 removed)', r.stdout)

    def test_sync_empty_source_after_the_legs(self):
        self.run_tw('sync', '-', now='09:00', stdin=json.dumps({'events': [self.LEG]}))
        r = self.run_tw('sync', '-', now='18:00', stdin=json.dumps({'events': []}))      # the leg is past
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_synced_leg_is_checked(self):
        link = ('https://app.sbbmobile.ch/timetable?from=Alphadorf&to=Betastadt%20HB&time='
                + str(int(dt.datetime(2026, 11, 14, 16, 31, tzinfo=CET).timestamp())))
        self.sync_events([{'id': 'e1', 'summary': '\U0001F686 S5 Alphadorf → Betastadt HB', 'description': f'SBB: {link}',
                           'start': {'dateTime': '2026-11-14T16:31:00+01:00'},
                           'end': {'dateTime': '2026-11-14T17:23:00+01:00'}}])
        self.srv.timetable = base(**{'16:31': s5('16:31', '17:23', new_pl='2')})
        self.check('15:00')
        txt = self.note_text()
        self.assertIn('\U0001F686 S5 Alphadorf → Betastadt HB', txt)
        self.assertIn('(calendar)', txt)

    def test_home_door_to_door(self):
        """A leg from the home address: the API gets its coordinates, the first bus is watched, lists say home."""
        with open(self.env['ASSISTANT_PROFILE'], 'w') as f:
            json.dump({'locale': {'tz': 'Europe/Zurich'},
                       'home': {'street': 'Probeweg 7', 'postcode': '1234', 'town': A}}, f)
        with open(self.env['HOME_PLACE_CACHE'], 'w') as f:
            json.dump({'key': hashlib.sha256(f'Probeweg 7, 1234 {A}'.encode()).hexdigest()[:16],
                       'lat': 47.5, 'lon': 8.5}, f)
        hq = '47.500000,8.500000'
        c = conn(ride('B', '7', f'{A}, Post', '16:24', A, '16:28', dd=6), ride('S', '5', A, '16:31', B, '17:23'))
        c['sections'].insert(0, {'walk': {'duration': 180}, 'journey': None,
                                 'departure': {'departure': t('16:20'), 'station': {'name': hq}, 'prognosis': {}},
                                 'arrival': {'arrival': t('16:23'), 'station': {'name': f'{A}, Post'}, 'prognosis': {}}})
        c['from'] = {'departure': t('16:20'), 'delay': None, 'station': {'name': hq}, 'prognosis': {}}
        self.srv.timetable = base() + [c]
        ev = {'id': 'evhome1', 'summary': '\U0001F686 Home → Betastadt HB',
              'start': {'dateTime': f'{DAY}T16:20:00+01:00'}, 'end': {'dateTime': f'{DAY}T17:23:00+01:00'},
              'description': f'16:20 walk 3 min › 16:24 Bus 7 {A}, Post › 16:31 S5 {A} pl 1 › 17:23 {B} · 1 change\n'
                             f'SBB: https://app.sbbmobile.ch/timetable?from={A}%2C%20Post'
                             f'&to=Betastadt%20HB&time=1794672240'}   # the link names the stop, the title says Home
        r = self.run_tw('sync', '-', now='09:00', stdin=json.dumps({'events': [ev]}))
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_tw('list', now='09:00')
        self.assertIn('16:20 → 17:23  home → Betastadt HB  [Bus 7 › S5]', r.stdout)
        self.assertNotIn('Probeweg', r.stdout)
        self.check('15:00')
        qs = self.lookups()
        self.assertTrue(qs and all(q[0] == hq for q in qs), qs)            # coordinates, never the address text
        n = self.note_text()                                             # the first bus is late: one note
        self.assertIn('(+6 min)', n)
        self.assertIn('home → Betastadt HB', n)
        self.assertIn(f'from={A}%2C%20Post&to=Betastadt%20HB', n)       # SBB link from the first stop
        self.assertNotIn('Probeweg', n)                                  # never the address (the app can't)
        self.assertIsNone(self.check('15:05').stderr or None)
        self.assertEqual(len(self.notes()), 1)                           # same problem: no second note

    def test_same_departure_two_ways_on(self):
        """Two connections share the departure and first line: the one with the planned arrival is the plan."""
        slow = conn(ride('S', '5', A, '16:31', C, '16:50'), ride('S', '9', C, '16:55', B, '17:37'))
        fast = conn(ride('S', '5', A, '16:31', C, '16:50'), ride('IC', '1', C, '16:52', B, '17:23'))
        self.srv.timetable = [slow, fast]
        lid = self.add_leg(dep='16:31', arr='17:23', lines=('S5', 'IC1'))
        r = self.run_tw('status', lid, now='15:00')
        self.assertIn('IC1 → 17:23', r.stdout)
        self.check('15:00')
        self.assertEqual(self.notes(), [])                               # no false timetable change

    # ------------------------------------------------------------------ the autumn clock change (CEST -> CET at 03:00)
    DST = '2031-10-26'                                  # 02:00-02:59 runs twice that night

    def dst_leg(self, dep, arr):
        """One leg A -> B written straight into legs.json (exact offsets, e.g. 02:20 in the second pass)."""
        g = {'id': 'cdst0001', 'from': A, 'to': B, 'dep': dep, 'arr': arr, 'lines': ['S5'], 'title': 'DST leg',
             'source': 'calendar', 'event_id': 'dstev'}
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, 'legs.json'), 'w') as f:
            json.dump({'legs': [g]}, f)
        return g['id']

    def run_at(self, *args, at):
        env = dict(self.env, TRAIN_WATCH_NOW=at)
        return subprocess.run([sys.executable, BIN, *args], env=env, capture_output=True, text=True, timeout=60)

    def test_clock_change_leg_ahead_in_second_pass(self):
        """02:30 CEST is 50 real minutes before a 02:20 CET leg: checked, not taken for gone (wall clocks say gone)."""
        d = self.DST
        r5 = ride('S', '5', A, '02:20', B, '03:12')
        r5['departure']['departure'], r5['arrival']['arrival'] = f'{d}T02:20:00+0100', f'{d}T03:12:00+0100'
        self.srv.timetable = [conn(r5)]
        lid = self.dst_leg(f'{d}T02:20:00+01:00', f'{d}T03:12:00+01:00')
        r = self.run_at('check', '--cron', at=f'{d}T02:30:00+02:00')
        self.assertEqual((r.returncode, r.stderr), (0, ''))
        qs = self.lookups()
        self.assertTrue(qs, 'the leg 50 min ahead was not checked')
        self.assertEqual(qs[0][2:], (d, '02:20'))                        # the API gets his wall time
        self.assertEqual(self.notes(), [])                                 # on time: silent
        j = json.loads(self.run_at('status', lid, '--json', at=f'{d}T02:30:00+02:00').stdout)
        self.assertTrue(j.get('fine'), j)

    def test_clock_change_leg_gone_in_second_pass(self):
        """02:10 CET is 30 real minutes after a 02:40 CEST leg left: no check, no note (wall clocks say 30 min ahead)."""
        d = self.DST
        self.srv.timetable = []
        self.dst_leg(f'{d}T02:40:00+02:00', f'{d}T03:32:00+01:00')
        r = self.run_at('check', '--cron', at=f'{d}T02:10:00+01:00')
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '', ''))
        self.assertEqual([p for p, q in self.srv.requests], [])
        self.assertEqual(self.notes(), [])

    def test_clock_change_add_arrival_after_the_change(self):
        """add 01:50 (CEST) --arr 03:10: the arrival is 03:10 CET on his wall clock, 80 real minutes later."""
        r = self.run_at('add', A, B, f'{self.DST} 01:50', '--arr', '03:10', at='2031-10-25T12:00:00+02:00')
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.dir, 'legs.json')) as f:
            g = json.load(f)['legs'][0]
        self.assertEqual((g['dep'], g['arr']), (f'{self.DST}T01:50:00+02:00', f'{self.DST}T03:10:00+01:00'))



PHONE_STUB = """#!/bin/sh
echo "$*" >> "$FAKE_PHONE_LOG"
rc=$(cat "$FAKE_PHONE_RC" 2>/dev/null || echo 0)
if [ "$rc" = 4 ]; then echo '{"ok": false, "status": "no_device"}'; else echo '{"ok": true, "status": "sent"}'; fi
exit $rc
"""


class Live(TW):
    """The phone app's Live Update chip (livecount) with a stub phone: 30 min before departure, updated on delay,
    ended when cancelled or gone; silent without LIVECOUNT_PHONE in test state."""
    def setUp(self):
        super().setUp()
        d = self.tmp.name
        self.plog, self.prc = os.path.join(d, 'phone.log'), os.path.join(d, 'phone.rc')
        stub = os.path.join(d, 'phone')
        with open(stub, 'w') as f:
            f.write(PHONE_STUB)
        os.chmod(stub, 0o755)
        self.env.update(LIVECOUNT_PHONE=stub, LIVECOUNT_DIR=os.path.join(d, 'live'), FAKE_PHONE_LOG=self.plog,
                        FAKE_PHONE_RC=self.prc)

    def calls(self):
        try:
            with open(self.plog) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def epoch(self, hhmm):
        return int(dt.datetime.fromisoformat(f'{DAY}T{hhmm}:00+01:00').timestamp())

    def test_chip_starts_30_min_before_and_updates_on_delay(self):
        self.add_leg()
        self.check('15:30')
        self.assertEqual(self.calls(), [])
        self.check('16:05')
        c = self.calls()
        self.assertEqual(len(c), 1, c)
        self.assertTrue(c[0].startswith('push --level info --title Train 16:31 --body S5 · platform 1 --tag live:train:'), c)
        self.assertIn(f"--live countdown:{self.epoch('16:31')}", c[0])
        self.check('16:15')
        self.assertEqual(len(self.calls()), 1, 'unchanged: no second push')
        self.srv.timetable = base(**{'16:31': s5('16:31', '17:23', dd=6, ad=6)})
        self.check('16:25')
        c = self.calls()
        self.assertEqual(len(c), 2, c)
        self.assertIn('--title Train 16:31 +6 min', c[1])
        self.assertIn(f"--live countdown:{self.epoch('16:37')}", c[1])
        self.assertEqual(c[0].split('--tag ')[1].split()[0], c[1].split('--tag ')[1].split()[0], 'same tag: updated')
        self.check('16:34')                       # past the scheduled time: out of the window, the chip is held
        self.assertEqual(len(self.calls()), 2)
        self.check('16:40')                       # the late train has left (+2 min grace): ended
        c = self.calls()
        self.assertEqual(len(c), 3, c)
        self.assertTrue(c[2].startswith('cancel live:train:'), c)

    def test_cancelled_train_ends_the_chip(self):
        self.add_leg()
        self.check('16:05')
        self.srv.timetable = base(**{'16:31': s5('16:31', '17:23', cancelled=True)})
        self.check('16:15')
        c = self.calls()
        self.assertEqual(len(c), 2, c)
        self.assertTrue(c[1].startswith('cancel live:train:'), c)

    def test_door_leg_counts_down_to_leaving(self):
        with open(self.env['ASSISTANT_PROFILE'], 'w') as f:
            json.dump({'locale': {'tz': 'Europe/Zurich'},
                       'home': {'street': 'Probeweg 7', 'postcode': '1234', 'town': A}}, f)
        with open(self.env['HOME_PLACE_CACHE'], 'w') as f:
            json.dump({'key': hashlib.sha256(f'Probeweg 7, 1234 {A}'.encode()).hexdigest()[:16],
                       'lat': 47.5, 'lon': 8.5}, f)
        hq = '47.500000,8.500000'
        c = conn(ride('B', '7', f'{A}, Post', '16:24', A, '16:28', dd=3), ride('S', '5', A, '16:31', B, '17:23'))
        c['sections'].insert(0, {'walk': {'duration': 180}, 'journey': None,
                                 'departure': {'departure': t('16:20'), 'station': {'name': hq}, 'prognosis': {}},
                                 'arrival': {'arrival': t('16:23'), 'station': {'name': f'{A}, Post'}, 'prognosis': {}}})
        c['from'] = {'departure': t('16:20'), 'delay': None, 'station': {'name': hq}, 'prognosis': {}}
        self.srv.timetable = base() + [c]
        ev = {'id': 'evhome1', 'summary': '\U0001F686 Home → Betastadt HB',
              'start': {'dateTime': f'{DAY}T16:20:00+01:00'}, 'end': {'dateTime': f'{DAY}T17:23:00+01:00'},
              'description': f'16:20 walk 3 min › 16:24 Bus 7 {A}, Post › 16:31 S5 {A} pl 1 › 17:23 {B} · 1 change'}
        self.assertEqual(self.run_tw('sync', '-', now='09:00', stdin=json.dumps({'events': [ev]})).returncode, 0)
        self.check('16:00')
        c = self.calls()
        self.assertEqual(len(c), 1, c)
        self.assertTrue(c[0].startswith('push --level info --title Leave 16:20 --body '), c)
        self.assertIn(f"--live countdown:{self.epoch('16:20')}", c[0])
        self.assertNotIn('platform', c[0])
        self.assertNotIn('Probeweg', c[0])

    def test_no_device_is_silent_and_backs_off(self):
        with open(self.prc, 'w') as f:
            f.write('4')
        self.add_leg()
        r1, r2 = self.check('16:05'), self.check('16:13')
        self.assertEqual((r1.returncode, r2.returncode, r1.stdout + r2.stdout), (0, 0, ''))
        self.assertEqual(len(self.calls()), 1)

    def test_no_stub_in_test_state_no_phone(self):
        for k in ('LIVECOUNT_PHONE',):
            self.env.pop(k)
        self.add_leg()
        self.check('16:05')
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, 'live')))

    def test_dry_run_prints_the_chip(self):
        self.add_leg()
        r = self.run_tw('check', '--dry-run', now='16:05')
        self.assertIn('live: start chip "Train 16:31" (S5 · platform 1) until 16:31', r.stdout)
        self.assertEqual(self.calls(), [])


class SearchCh(TW):
    """Every TW test again with opendata down (200 + {"errors": ...}): the same verdicts through search.ch."""
    MODE = 'down'

    def test_platform_change(self):                     # search.ch marks the new track ("4!"), not the old one
        self.add_leg()
        self.srv.timetable = base(**{'16:31': s5('16:31', '17:23', new_pl='4')})
        self.check('15:00')
        self.assertIn(f'platform change at {A}: S5 now leaves from pl 4', self.note_text())
        self.check('15:10')
        self.assertEqual(len(self.notes()), 1)


class LiveSearchCh(Live):
    MODE = 'down'


class Fallback(TW):
    """The switch: opendata failing (error payload, 503, unreachable) -> search.ch, asked once per run; logged."""
    def sources(self):
        try:
            with open(os.path.join(self.dir, 'sources.log')) as f:
                return f.read()
        except OSError:
            return ''

    def paths(self):
        return [p.rsplit('/', 1)[-1] for p, _ in self.srv.requests]

    def test_opendata_answers_no_fallback(self):
        self.add_leg()
        self.srv.timetable = base(**{'16:31': s5('16:31', '17:23', dd=7, ad=7)})
        r = self.check('15:00')
        self.assertEqual((r.returncode, r.stderr), (0, ''))
        self.assertNotIn('route.json', self.paths())
        self.assertIn(' opendata', self.sources())
        self.assertNotIn('search.ch', self.sources())

    def test_router_error_payload_falls_back(self):
        self.add_leg()
        self.srv.mode = 'down'
        self.srv.timetable = base(**{'16:31': s5('16:31', '17:23', dd=7, ad=7)})
        r = self.check('15:00')
        self.assertEqual((r.returncode, r.stderr), (0, ''))             # --cron: quiet
        self.assertIn('delayed', self.note_text())
        self.assertIn('+7 min', self.note_text())
        p = self.paths()
        self.assertEqual(p.count('connections'), 1, p)                   # the broken source once per run
        self.assertEqual(p[0], 'connections')
        self.assertGreaterEqual(p.count('route.json'), 2, p)             # the leg, then the alternative
        q = [q for x, q in self.srv.requests if x.endswith('/route.json')][0]
        self.assertEqual((q['date'], q['time'], q['show_delays'], q['pre']), ('14.11.2026', '16:31', '1', '2'))
        self.assertIn('search.ch (opendata: transport API error: The router is not reachable)', self.sources())

    def test_503_falls_back_and_status_says_so(self):
        lid = self.add_leg()
        self.srv.mode = 'down503'
        r = self.run_tw('status', lid, now='15:00')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Live: 16:31 S5 → 17:23, pl 1, direct', r.stdout)
        self.assertIn('train-watch: via search.ch', r.stderr)

    def test_unreachable_falls_back(self):
        self.add_leg()
        self.env.update(TRAIN_WATCH_API='http://127.0.0.1:9/v1', no_proxy='127.0.0.1', NO_PROXY='127.0.0.1')
        self.srv.timetable = base(**{'16:31': None})
        self.check('16:10')                                              # under 25 min: gone at once
        self.assertIn('gone from the live timetable', self.note_text())
        self.assertIn('search.ch', self.sources())

    def test_both_down_exit_1(self):
        self.add_leg()
        self.srv.mode = 'error'
        r = self.check('15:00')
        self.assertEqual(r.returncode, 1)
        self.assertIn('backend down; fallback search.ch: transport API error: backend down', r.stderr)
        self.assertEqual(self.notes(), [])

    def test_searchch_cancelled_x(self):
        self.add_leg()
        self.srv.mode = 'down'
        self.srv.timetable = base(**{'16:31': s5('16:31', '17:23', cancelled=True)})
        self.check('15:00')
        self.assertIn('cancelled', self.note_text())


for _n in [n for n in dir(TW) if n.startswith('test')]:
    setattr(Live, _n, None)                       # Live reuses TW's fixtures, not its tests
    if _n not in vars(Fallback):
        setattr(Fallback, _n, None)


if __name__ == '__main__':
    unittest.main(verbosity=1)
