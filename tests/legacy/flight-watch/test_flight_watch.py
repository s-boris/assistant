"""Offline tests for ~/bin/flight-watch (FLIGHT_WATCH_BIN=/path/to/copy tests a copy; CALCACHE_BIN for calcache).

A loopback fake serves both sources in their page shapes: FlightStats' flight tracker (HTML with `__NEXT_DATA__ = {..}`:
flight + otherDays) and FlightAware's flight page (HTML with `var trackpollBootstrap = {..}`, ICAO idents). Each test
sets the fake operations; every request is recorded. http(s)_proxy point at the fake too, and HOME is a temp dir, so
nothing leaves the box and no real state, inbox or cache is touched. Fictional flights (QR 9101 FRA → DOH, QR 9672
DOH → MLE) and a synthetic profile only.
Covers: cron fast path, cadence (daily / hourly / every run, milestones, stop after landing), calendar sync (legs from
description lines, dates carried over, title-only flight, rail lines, all-day and cancelled skipped, removed events,
manual flights kept, sync --from-cache equal to sync FILE, far flights kept), recent-days timetable notes (8-day limit,
same IATA season), on-date schedule change, delay buckets + dedup, cancelled, missing (two runs / one when close),
gate + terminal changes (first assignment silent), connection at risk, FlightStats down -> FlightAware, both down,
dry run, status, list with last check, add/rm/usage errors, note naming and relay wording.
"""
import datetime as dt
import glob
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from zoneinfo import ZoneInfo

BIN = os.environ.get('FLIGHT_WATCH_BIN') or os.path.expanduser('~/bin/flight-watch')
CALCACHE = os.environ.get('CALCACHE_BIN') or os.path.expanduser('~/bin/calcache')
REAL_INBOX = os.path.expanduser('~/inbox')
Z = {'FRA': ZoneInfo('Europe/Berlin'), 'DOH': ZoneInfo('Asia/Qatar'), 'MLE': ZoneInfo('Indian/Maldives')}
UTC = dt.timezone.utc


def at(code, s):
    """at('FRA', '2026-11-14 16:10') -> aware local time at that airport."""
    return dt.datetime.fromisoformat(s).replace(tzinfo=Z[code])


def op(no, src, dst, dep, arr, **kw):
    o = {'no': no, 'from': src, 'to': dst, 'sdep': at(src, dep), 'sarr': at(dst, arr), 'ddelay': 0, 'adelay': 0,
         'gate': None, 'terminal': None, 'cancelled': False, 'diverted': False, 'landed': False, 'departed': False,
         'id': abs(hash((no, dep))) % 10 ** 9}
    o.update(kw)
    return o


def leg1(day='2026-11-14', **kw):
    nxt = (dt.date.fromisoformat(day)).isoformat()
    return op('QR9101', 'FRA', 'DOH', f'{day} 16:10', f'{nxt} 23:40', **kw)


def leg2(day='2026-11-15', **kw):
    return op('QR9672', 'DOH', 'MLE', f'{day} 01:05', f'{day} 07:50', **kw)


def utcz(t):
    return t.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def loc(t):
    return t.strftime('%Y-%m-%dT%H:%M:%S.000')


def fs_flight(o):
    edep, earr = o['sdep'] + dt.timedelta(minutes=o['ddelay']), o['sarr'] + dt.timedelta(minutes=o['adelay'])
    code = 'C' if o['cancelled'] else 'D' if o['diverted'] else 'L' if o['landed'] else 'S'
    return {'schedule': {'scheduledDeparture': loc(o['sdep']), 'scheduledDepartureUTC': utcz(o['sdep']),
                         'estimatedActualDeparture': loc(edep), 'estimatedActualDepartureUTC': utcz(edep),
                         'estimatedActualDepartureTitle': 'Actual' if o['departed'] else 'Estimated',
                         'scheduledArrival': loc(o['sarr']), 'scheduledArrivalUTC': utcz(o['sarr']),
                         'estimatedActualArrival': loc(earr), 'estimatedActualArrivalUTC': utcz(earr),
                         'estimatedActualArrivalTitle': 'Actual' if o['landed'] else 'Estimated'},
            'status': {'statusCode': code, 'status': {'C': 'Cancelled', 'D': 'Diverted', 'L': 'Landed'}.get(code, 'Scheduled'),
                       'diverted': o['diverted']},
            'flightNote': {'canceled': o['cancelled'], 'landed': o['landed']},
            'departureAirport': {'iata': o['from'], 'timeZoneRegionName': str(Z[o['from']]), 'gate': o['gate'],
                                 'terminal': o['terminal']},
            'arrivalAirport': {'iata': o['to'], 'timeZoneRegionName': str(Z[o['to']]), 'terminal': None}}


def fa_entry(o):
    ep = lambda t: int(t.timestamp())  # noqa: E731
    edep, earr = o['sdep'] + dt.timedelta(minutes=o['ddelay']), o['sarr'] + dt.timedelta(minutes=o['adelay'])
    return {'origin': {'iata': o['from'], 'TZ': f":{Z[o['from']]}", 'gate': o['gate'], 'terminal': o['terminal']},
            'destination': {'iata': o['to'], 'TZ': f":{Z[o['to']]}", 'gate': None, 'terminal': None},
            'gateDepartureTimes': {'scheduled': ep(o['sdep']), 'estimated': ep(edep),
                                   'actual': ep(edep) if o['departed'] else None},
            'gateArrivalTimes': {'scheduled': ep(o['sarr']), 'estimated': ep(earr),
                                 'actual': ep(earr) if o['landed'] else None},
            'cancelled': o['cancelled'], 'diverted': o['diverted'], 'flightStatus': 'arrived' if o['landed'] else ''}


class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        srv = self.server
        srv.requests.append((u.path, q))
        parts = u.path.strip('/').split('/')
        if parts[:2] == ['v2', 'flight-tracker'] and len(parts) == 4:
            if srv.fs_mode == 'error':
                return self.send(503, b'down')
            if srv.fs_mode == 'junk':
                return self.send(200, b'<html>no data</html>')
            no = parts[2] + parts[3]
            day = dt.date(int(q['year']), int(q['month']), int(q['date']))
            ops = [o for o in srv.ops if o['no'] == no]
            pick = [o for o in ops if o['sdep'].date() == day and (not q.get('flightId') or str(o['id']) == q['flightId'])]
            days = {}
            for o in ops:
                days.setdefault(o['sdep'].date(), []).append(
                    {'departureAirport': {'iata': o['from']}, 'arrivalAirport': {'iata': o['to']},
                     'departureTime24': o['sdep'].strftime('%H:%M'), 'arrivalTime24': o['sarr'].strftime('%H:%M'),
                     'sortTime': utcz(o['sdep']),
                     'url': f"/flight-tracker/{parts[2]}/{parts[3]}?year={o['sdep'].year}&month={o['sdep'].month}"
                            f"&date={o['sdep'].day}&flightId={o['id']}"})
            data = {'props': {'initialState': {'flightTracker': {
                'flight': fs_flight(pick[0]) if pick else {}, 'route': {'flights': []},
                'otherDays': [{'date1': d.strftime('%d-%b'), 'year': str(d.year), 'flights': v}
                              for d, v in sorted(days.items())]}}}}
            return self.send(200, ('<html><script>__NEXT_DATA__ = ' + json.dumps(data) +
                                   ';__NEXT_LOADED_PAGES__=[];</script></html>').encode())
        if parts[:2] == ['live', 'flight'] and len(parts) == 3:
            if srv.fa_mode == 'error':
                return self.send(500, b'down')
            ident = parts[2]
            ops = [o for o in srv.ops if o['no'].replace('QR', 'QTR', 1) == ident]
            if not ops:
                flights = {f'INVALID-{ident}': {'activityLog': None}}
            else:
                latest = max(ops, key=lambda o: o['sdep'])
                flights = {f'{ident}-1-airline-0': dict(fa_entry(latest),
                                                        activityLog={'flights': [fa_entry(o) for o in ops]})}
            boot = {'version': '1', 'summary': False, 'flights': flights}
            return self.send(200, ('<html><script>var trackpollBootstrap = ' + json.dumps(boot) +
                                   ';</script></html>').encode())
        return self.send(404, b'not found')

    def send(self, code, body):
        self.send_response(code)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(body)


EVENT_DESC = ('Fictional test trip; times from the e-tickets.\n'
              'QR 9101 Frankfurt Sat 14.11 16:10 → Doha Sat 14.11 23:40 (local)\n'
              'QR 9672 Doha 01:05 → Malé (MLE) 07:50 local time (= 03:50 CET). Connection 1h25.\n'
              'IC 5 Basel 12:00 → Frankfurt 15:00\n'
              'To the airport: leave at 13:00.')


def events(extra=()):
    ev = [{'id': 'evtrip1', 'status': 'confirmed', 'summary': '✈️ Flight Frankfurt → Doha → Malé',
           'start': {'dateTime': '2026-11-14T16:10:00+01:00'}, 'end': {'dateTime': '2026-11-15T03:50:00+01:00'},
           'description': EVENT_DESC},
          {'id': 'evtitle', 'status': 'confirmed', 'summary': '✈️ QR 9333 Doha → Frankfurt',
           'start': {'dateTime': '2026-11-20T09:00:00+03:00'}, 'end': {'dateTime': '2026-11-20T13:30:00+01:00'}},
          {'id': 'evallday', 'status': 'confirmed', 'summary': '✈️ QR 9555 Frankfurt → Doha',
           'start': {'date': '2026-11-16'}, 'end': {'date': '2026-11-17'}},
          {'id': 'evcanc', 'status': 'cancelled', 'summary': '✈️ QR 9777 Frankfurt → Doha',
           'start': {'dateTime': '2026-11-18T10:00:00+01:00'}, 'end': {'dateTime': '2026-11-18T18:00:00+03:00'}},
          {'id': 'evtrain', 'status': 'confirmed', 'summary': '🚆 Alphadorf → Betastadt',
           'start': {'dateTime': '2026-11-14T10:00:00+01:00'}, 'end': {'dateTime': '2026-11-14T11:00:00+01:00'}}]
    return {'kind': 'calendar#events', 'events': ev + list(extra)}


class FW(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fake)
        cls.srv.requests, cls.srv.ops, cls.srv.fs_mode, cls.srv.fa_mode = [], [], 'ok', 'ok'
        cls.url = f'http://127.0.0.1:{cls.srv.server_port}'
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.real_notes = set(glob.glob(os.path.join(REAL_INBOX, 'flight-watch-*')))

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        assert set(glob.glob(os.path.join(REAL_INBOX, 'flight-watch-*'))) <= cls.real_notes, 'touched the real inbox'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.home = os.path.join(d, 'home')
        os.makedirs(self.home)
        self.dir, self.inbox, self.cal = (os.path.join(d, x) for x in ('state', 'inbox', 'calcache'))
        prof = os.path.join(d, 'profile.json')
        with open(prof, 'w') as f:
            json.dump({'locale': {'tz': 'Europe/Zurich'}, 'home': {'town': 'Alphadorf'}}, f)
        self.env = dict(os.environ, HOME=self.home, FLIGHT_WATCH_DIR=self.dir, FLIGHT_WATCH_INBOX=self.inbox,
                        FLIGHT_WATCH_FS=self.url + '/v2', FLIGHT_WATCH_FA=self.url, CALCACHE_DIR=self.cal,
                        ASSISTANT_PROFILE=prof, http_proxy=self.url, https_proxy=self.url, HTTP_PROXY=self.url,
                        HTTPS_PROXY=self.url, no_proxy='127.0.0.1', NO_PROXY='127.0.0.1',
                        HOME_PLACE_GEO='off', HOME_PLACE_CACHE=os.path.join(d, 'home-place.json'))
        for k in ('FLIGHT_WATCH_NOW', 'FLIGHT_WATCH_SOURCES', 'CALCACHE_NOW', 'FLIGHTS_EXTRA'):
            self.env.pop(k, None)
        self.srv.requests.clear()
        self.srv.ops, self.srv.fs_mode, self.srv.fa_mode = [], 'ok', 'ok'

    def tearDown(self):
        self.tmp.cleanup()

    # ---------------------------------------------------------------- helpers
    def run_fw(self, *args, now=None, stdin=None, env=None):
        e = dict(self.env, **(env or {}))
        if now:
            e['FLIGHT_WATCH_NOW'] = now
        r = subprocess.run([sys.executable, BIN, *args], env=e, capture_output=True, text=True, input=stdin,
                           timeout=60)
        self.assertNotIn('Traceback', r.stderr)
        return r

    def sync(self, data=None, now='2026-11-01T08:00:00+01:00'):
        r = self.run_fw('sync', '-', now=now, stdin=json.dumps(data or events()))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def flights(self, now='2026-11-01T08:00:00+01:00'):
        r = self.run_fw('list', '--json', now=now)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def ids(self):
        fs = self.flights()
        return {f['no']: f['id'] for f in fs}

    def check(self, now, *extra):
        self.srv.requests.clear()
        return self.run_fw('check', '--cron', *extra, now=now)

    def notes(self):
        return sorted(glob.glob(os.path.join(self.inbox, 'flight-watch-*.md')))

    def note(self, i=-1):
        with open(self.notes()[i]) as f:
            return f.read()

    def state(self):
        with open(os.path.join(self.dir, 'state.json')) as f:
            return json.load(f)['flights']

    # ---------------------------------------------------------------- basics
    def test_help(self):
        r = self.run_fw('--help')
        self.assertEqual(r.returncode, 0)
        for w in ('sync', 'add', 'check', 'status', 'list'):
            self.assertIn(w, r.stdout)

    def test_no_flights_is_silent(self):
        r = self.check('2026-11-14T12:00:00+01:00')
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '', ''))
        self.assertEqual(self.srv.requests, [])
        self.assertFalse(os.path.exists(self.inbox))

    # ---------------------------------------------------------------- sync
    def test_sync_legs_from_description(self):
        r = self.sync()
        self.assertIn('synced 3 calendar flights', r.stdout)
        fs = {f['no']: f for f in self.flights()}
        self.assertEqual(set(fs), {'QR9101', 'QR9672', 'QR9333'})       # no rail line, no all-day, no cancelled
        a, b = fs['QR9101'], fs['QR9672']
        self.assertEqual((a['from'], a['to'], a['dep'], a['arr']),
                         ('FRA', 'DOH', '2026-11-14T16:10:00+01:00', '2026-11-14T23:40:00+03:00'))
        self.assertEqual((b['from'], b['to'], b['dep'], b['arr']),       # date carried over past midnight
                         ('DOH', 'MLE', '2026-11-15T01:05:00+03:00', '2026-11-15T07:50:00+05:00'))
        self.assertEqual(a['next'], b['id'])
        t = fs['QR9333']                                                 # title-only: event start/end
        self.assertEqual((t['from'], t['to'], t['dep']), ('DOH', 'FRA', '2026-11-20T09:00:00+03:00'))
        self.assertEqual(self.srv.requests, [])

    def test_sync_replaces_calendar_keeps_manual(self):
        self.sync()
        r = self.run_fw('add', 'QR 9888', '2026-11-25', '--from', 'Doha', '--to', 'FRA', '--dep', '08:00',
                        '--arr', '12:30', now='2026-11-01T08:00:00+01:00')
        self.assertEqual(r.returncode, 0, r.stderr)
        data = events()
        data['events'] = [e for e in data['events'] if e['id'] != 'evtitle']
        r = self.sync(data)
        self.assertIn('(1 removed)', r.stdout)
        self.assertIn('kept 1 added by hand', r.stdout)
        self.assertEqual({f['no'] for f in self.flights()}, {'QR9101', 'QR9672', 'QR9888'})

    def test_sync_from_cache_equals_file(self):
        self.sync()
        by_file = {f['id']: (f['no'], f['from'], f['to'], f['dep'], f['arr'], f['next']) for f in self.flights()}
        env = dict(self.env, CALCACHE_NOW='2026-11-01T08:00:00+01:00')
        r = subprocess.run([sys.executable, CALCACHE, 'import', '-', '--days', '30'], env=env, input=json.dumps(events()),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.cal, 'events.json')) as f:
            cached = json.load(f)['events']
        self.assertTrue(all('description' not in e for e in cached))
        self.assertEqual(len([e for e in cached if e.get('flight')]), 2)
        os.remove(os.path.join(self.dir, 'flights.json'))
        r = self.run_fw('sync', '--from-cache', now='2026-11-01T08:00:00+01:00')
        self.assertEqual(r.returncode, 0, r.stderr)
        by_cache = {f['id']: (f['no'], f['from'], f['to'], f['dep'], f['arr'], f['next']) for f in self.flights()}
        self.assertEqual(by_cache, by_file)

    def test_sync_from_cache_keeps_flights_past_window(self):
        self.sync()
        env = dict(self.env, CALCACHE_NOW='2026-11-01T08:00:00+01:00')
        subprocess.run([sys.executable, CALCACHE, 'import', '-', '--days', '16'], env=env,
                       input=json.dumps(events()), capture_output=True, text=True, timeout=60, check=True)
        r = self.run_fw('sync', '--from-cache', now='2026-11-01T08:00:00+01:00')
        self.assertIn('kept 1 past the cache window', r.stdout)                  # QR9333 on 20.11
        self.assertEqual(len(self.flights()), 3)

    def flights_file(self):
        with open(os.path.join(self.dir, 'flights.json')) as f:
            return f.read()

    def test_sync_items_key(self):
        self.sync()
        by_events = self.flights()
        os.remove(os.path.join(self.dir, 'flights.json'))
        # the Google API shape: events under "items", the page's own "summary" is the calendar's name
        self.sync({'kind': 'calendar#events', 'summary': 'Primary', 'items': events()['events']})
        self.assertEqual(self.flights(), by_events)
        r = self.run_fw('sync', '-', now='2026-11-01T08:00:00+01:00', stdin=json.dumps({'summary': 'Primary'}))
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('no list_events result', r.stderr)

    def test_sync_empty_source_keeps_flights(self):
        now = '2026-11-01T08:00:00+01:00'
        self.sync()
        before = self.flights_file()
        r = self.run_fw('sync', '-', now=now, stdin=json.dumps({'items': []}))
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('keeping the 3 upcoming calendar flights', r.stderr)
        env = dict(self.env, CALCACHE_NOW=now)
        subprocess.run([sys.executable, CALCACHE, 'import', '-'], env=env, input=json.dumps({'events': []}),
                       capture_output=True, text=True, timeout=60, check=True)       # no cache before: accepted
        r = self.run_fw('sync', '--from-cache', now=now)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('the calendar cache holds no events at all', r.stderr)
        self.assertEqual(self.flights_file(), before)
        r = self.run_fw('sync', '--from-cache', '--allow-empty', now=now)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.flights(), [])

    def test_private_airport_extras(self):
        extra = os.path.join(self.tmp.name, 'airports.json')
        with open(extra, 'w') as f:
            json.dump({'airports': {'XQA': ['Asia/Qatar', 'testport'], 'BAD': ['No/Zone', 'x']},
                       'airlines': {'ZQ': 'ZQX'}}, f)
        ev = {'id': 'evx', 'status': 'confirmed', 'summary': '✈️ Testport trip',
              'start': {'dateTime': '2026-11-21T10:00:00+03:00'}, 'end': {'dateTime': '2026-11-21T14:30:00+01:00'},
              'description': 'ZQ 77 Testport 10:00 → Frankfurt 14:30'}
        self.sync({'events': [ev]})
        self.assertEqual(self.flights(), [])                                     # unknown without the extras
        self.env['FLIGHTS_EXTRA'] = extra
        self.sync({'events': [ev]})
        f = self.flights()[0]
        self.assertEqual((f['from'], f['dep'], f['arr']), ('XQA', '2026-11-21T10:00:00+03:00',
                                                           '2026-11-21T14:30:00+01:00'))
        self.check('2026-11-20T12:00:00+01:00')
        self.assertIn('/live/flight/ZQX77', {p for p, _ in self.srv.requests})

    def test_add_rm_usage(self):
        now = '2026-11-01T08:00:00+01:00'
        self.assertEqual(self.run_fw('add', 'nonsense!', '2026-11-25', '--from', 'FRA', '--to', 'DOH', '--dep', '08:00',
                                     now=now).returncode, 2)
        self.assertEqual(self.run_fw('add', 'QR1', '2026-11-25', '--from', 'Atlantis', '--to', 'DOH', '--dep', '08:00',
                                     now=now).returncode, 2)
        self.assertEqual(self.run_fw('add', 'QR1', '2026-10-25', '--from', 'FRA', '--to', 'DOH', '--dep', '08:00',
                                     now=now).returncode, 2)
        r = self.run_fw('add', 'QR1', '2026-11-25', '--from', 'FRA', '--to', 'DOH', '--dep', '22:00', '--arr', '06:00',
                        now=now)
        self.assertEqual(r.returncode, 0, r.stderr)
        f = self.flights()[0]
        self.assertEqual(f['arr'], '2026-11-26T06:00:00+03:00')
        self.assertEqual(self.run_fw('rm', 'fnope', now=now).returncode, 2)
        self.assertEqual(self.run_fw('rm', f['id'], now=now).returncode, 0)
        self.assertEqual(self.flights(), [])

    # ---------------------------------------------------------------- cadence
    def test_daily_cadence_and_milestone(self):
        self.sync()
        r = self.check('2026-11-01T09:00:00+01:00')                       # 13 days ahead: first check
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.srv.requests)
        self.check('2026-11-01T10:00:00+01:00')
        self.assertEqual(self.srv.requests, [])                            # not due: no network
        self.check('2026-11-02T08:50:00+01:00')
        self.assertTrue(self.srv.requests)                                 # a day later
        self.run_fw('check', '--cron', now='2026-11-07T08:50:00+01:00')
        self.check('2026-11-07T16:20:00+01:00')                            # crossed 7 days before QR9101
        paths = {p for p, _ in self.srv.requests}
        self.assertIn('/live/flight/QTR9101', paths)                       # ICAO ident for FlightAware
        self.assertNotIn('/live/flight/QTR9333', paths)

    def test_hourly_then_every_run_then_stop(self):
        self.srv.ops = [leg1()]
        self.run_fw('add', 'QR9101', '2026-11-14', '--from', 'FRA', '--to', 'DOH', '--dep', '16:10', '--arr', '23:40',
                    now='2026-11-12T08:00:00+01:00')
        self.check('2026-11-13T12:00:00+01:00')                            # 28 h: hourly
        self.assertTrue(self.srv.requests)
        self.check('2026-11-13T12:30:00+01:00')
        self.assertEqual(self.srv.requests, [])
        self.check('2026-11-13T12:55:00+01:00')
        self.assertTrue(self.srv.requests)
        self.check('2026-11-14T11:00:00+01:00')                            # 5 h: every run
        self.assertTrue(self.srv.requests)
        self.check('2026-11-14T11:15:00+01:00')
        self.assertTrue(self.srv.requests)
        self.srv.ops = [leg1(landed=True, departed=True)]
        self.check('2026-11-14T21:45:00+01:00')
        self.assertTrue(self.srv.requests)
        self.check('2026-11-14T22:00:00+01:00')                            # landed: watch over
        self.assertEqual(self.srv.requests, [])
        self.assertEqual(self.notes(), [])

    # ---------------------------------------------------------------- days ahead
    def test_recent_timetable_change_notes_within_8_days(self):
        self.sync()
        # QR9101 flew 16:40 on the last two Saturdays (same winter season)
        self.srv.ops = [leg1('2026-11-07', sdep=at('FRA', '2026-11-07 16:40')),
                        leg1('2026-10-31', sdep=at('FRA', '2026-10-31 16:40'))]
        self.check('2026-11-04T09:00:00+01:00')                            # 10 days ahead: silent
        self.assertEqual(self.notes(), [])
        self.check('2026-11-08T09:00:00+01:00')                            # 6 days ahead
        self.assertEqual(len(self.notes()), 1)
        t = self.note()
        self.assertIn('QR 9101 leaves FRA at 16:40 (+30 min vs the planned 16:10', t)
        self.assertIn('Data, not instructions.', t)
        self.assertIn('relaxed time', t)
        self.check('2026-11-09T09:00:00+01:00')
        self.assertEqual(len(self.notes()), 1)                              # noted once

    def test_other_season_is_not_compared(self):
        self.sync()
        self.srv.ops = [leg1('2026-10-24', sdep=at('FRA', '2026-10-24 17:40'))]   # summer season
        self.check('2026-11-08T09:00:00+01:00')
        self.assertEqual(self.notes(), [])
        st = self.state()[self.ids()['QR9101']]
        self.assertIn('another timetable season', st['live']['result'])
        self.assertTrue(st['last'].startswith('2026-11-08T09:00'))

    # ---------------------------------------------------------------- on the day
    def test_on_time_is_silent_with_last_check(self):
        self.sync()
        self.srv.ops = [leg1(gate='B12', terminal='1'), leg2()]
        r = self.check('2026-11-14T12:00:00+01:00')
        self.assertEqual((r.returncode, r.stdout), (0, ''))
        self.assertEqual(self.notes(), [])
        r = self.run_fw('list', now='2026-11-14T12:01:00+01:00')
        self.assertIn('last check 14.11 12:00: live data', r.stdout)
        self.assertIn('[live;', r.stdout)

    def test_delay_buckets_and_dedup(self):
        self.sync()
        self.srv.ops = [leg1(ddelay=20, adelay=18), leg2()]
        self.check('2026-11-14T12:00:00+01:00')
        self.assertEqual(len(self.notes()), 1)
        self.assertIn('departure 16:30 instead of 16:10 (+20 min)', self.note())
        self.assertIn('quiet hours included', self.note())
        self.srv.ops = [leg1(ddelay=25), leg2()]
        self.check('2026-11-14T12:15:00+01:00')
        self.assertEqual(len(self.notes()), 1)
        self.srv.ops = [leg1(ddelay=70, adelay=65), leg2()]
        self.check('2026-11-14T12:30:00+01:00')
        self.assertEqual(len(self.notes()), 2)
        self.assertIn('+70 min', self.note())

    def test_cancelled(self):
        self.sync()
        self.srv.ops = [leg1(cancelled=True), leg2()]
        self.check('2026-11-14T12:00:00+01:00')
        self.assertIn('CANCELLED', self.note())
        self.check('2026-11-14T12:15:00+01:00')
        self.assertEqual(len(self.notes()), 1)                              # final: no more checks or notes

    def test_gate_and_terminal_changes(self):
        self.sync()
        self.srv.ops = [leg1(gate='B12', terminal='1'), leg2()]
        self.check('2026-11-14T12:00:00+01:00')
        self.assertEqual(self.notes(), [])                                  # first assignment is silent
        self.srv.ops = [leg1(gate='A7', terminal='2'), leg2()]
        self.check('2026-11-14T12:15:00+01:00')
        t = self.note()
        self.assertIn('gate changed: B12 → A7 (at FRA)', t)
        self.assertIn('terminal changed: 1 → 2', t)

    def test_schedule_change_on_its_date(self):
        self.sync()
        self.srv.ops = [leg1(sdep=at('FRA', '2026-11-14 16:45')), leg2()]
        self.check('2026-11-13T18:00:00+01:00')
        self.assertIn('timetable changed: departure 16:45 instead of 16:10 (+35 min', self.note())

    def test_missing_two_runs_then_note(self):
        self.sync()
        self.srv.ops = [leg1('2026-11-15'), leg2()]                         # flies the next day, not on 14.11
        self.check('2026-11-13T18:00:00+01:00')
        self.assertEqual(self.notes(), [])
        self.check('2026-11-13T19:00:00+01:00')
        self.assertIn('is not listed for Sat 14.11', self.note())

    def test_missing_close_to_departure_one_run(self):
        self.sync()
        self.srv.ops = [leg1('2026-11-15'), leg2()]
        self.check('2026-11-14T12:00:00+01:00')
        self.assertEqual(len(self.notes()), 1)

    def test_connection_at_risk(self):
        self.sync()
        self.srv.ops = [leg1(adelay=60), leg2()]                            # lands 00:40, QR9672 leaves 01:05
        self.check('2026-11-14T12:00:00+01:00')
        t = '\n'.join(open(p).read() for p in self.notes())
        self.assertIn('connection at risk: lands 00:40, QR 9672 leaves 01:05 (25 min to change at DOH)', t)

    # ---------------------------------------------------------------- sources
    def test_flightstats_down_uses_flightaware(self):
        self.sync()
        self.srv.fs_mode = 'error'
        self.srv.ops = [leg1(ddelay=40), leg2()]
        r = self.check('2026-11-14T12:00:00+01:00')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('(FlightAware)', self.note())
        self.assertIn('+40 min', self.note())

    def test_junk_page_degrades_quietly(self):
        self.sync()
        self.srv.fs_mode = 'junk'
        self.srv.ops = [leg1(), leg2()]
        r = self.check('2026-11-14T12:00:00+01:00')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.notes(), [])

    def test_all_sources_down(self):
        self.sync()
        self.srv.fs_mode = self.srv.fa_mode = 'error'
        r = self.check('2026-11-08T12:00:00+01:00')                         # daily phase
        self.assertEqual(r.returncode, 1)
        r = self.check('2026-11-08T12:15:00+01:00')
        self.assertEqual(r.returncode, 1)
        r = self.check('2026-11-14T12:00:00+01:00')
        self.assertEqual(r.returncode, 1)
        self.assertIn('HTTP', r.stderr)
        self.assertEqual(self.notes(), [])
        self.assertTrue(self.state()[self.ids()['QR9101']]['live']['result'].startswith('error'))
        r = self.check('2026-11-14T12:15:00+01:00')                         # retried at once, not after a day
        self.assertEqual(r.returncode, 1)
        self.assertTrue(self.srv.requests)
        self.srv.fs_mode = self.srv.fa_mode = 'ok'
        self.srv.ops = [leg1(), leg2()]
        r = self.check('2026-11-14T12:30:00+01:00')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state()[self.ids()['QR9101']]['live']['result'], 'live data')

    def test_unknown_to_flightaware_is_no_data(self):
        self.sync()
        self.srv.ops = []
        self.env['FLIGHT_WATCH_SOURCES'] = 'fa'
        r = self.check('2026-11-08T09:00:00+01:00')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.notes(), [])

    # ---------------------------------------------------------------- dry run, status
    def test_dry_run_writes_nothing(self):
        self.sync()
        self.srv.ops = [leg1(cancelled=True), leg2()]
        r = self.run_fw('check', '--dry-run', now='2026-11-14T12:00:00+01:00')
        self.assertIn('CANCELLED', r.stdout)
        self.assertEqual(self.notes(), [])
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'state.json')) and self.state())

    def test_status_json_writes_no_state(self):
        self.sync()
        self.srv.ops = [leg1(ddelay=35, gate='B3'), leg2()]
        r = self.run_fw('status', self.ids()['QR9101'], '--json', now='2026-11-14T12:00:00+01:00')
        self.assertEqual(r.returncode, 0, r.stderr)
        j = json.loads(r.stdout)
        self.assertFalse(j['fine'])
        self.assertEqual(j['gate'], 'B3')
        self.assertEqual([p['key'] for p in j['problems']], ['delay30'])
        self.assertEqual(self.notes(), [])
        self.assertFalse(self.state())

    def test_force_checks_everything_now(self):
        self.sync()
        r = self.run_fw('check', '--force', now='2026-11-01T09:00:00+01:00')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.strip().splitlines()), 3)
        r = self.run_fw('check', '--force', now='2026-11-01T09:05:00+01:00')
        self.assertEqual(len(r.stdout.strip().splitlines()), 3)

    # ---------------------------------------------------------------- the autumn clock change (CEST -> CET at 03:00)
    def test_clock_change_delay_in_real_minutes(self):
        """ZRH 02:50 CEST, now expected 02:10 CET: +20 real minutes (wall clocks in one zone said -40, no note)."""
        Z.setdefault('ZRH', ZoneInfo('Europe/Zurich'))
        now = '2031-10-25T12:00:00+02:00'
        r = self.run_fw('add', 'QR9001', '2031-10-26', '--from', 'ZRH', '--to', 'DOH', '--dep', '02:50', '--arr', '10:30',
                        now=now)
        self.assertEqual(r.returncode, 0, r.stderr)
        f = self.flights(now=now)[0]
        self.assertEqual((f['dep'], f['arr']), ('2031-10-26T02:50:00+02:00', '2031-10-26T10:30:00+03:00'))
        cest, ast = dt.timezone(dt.timedelta(hours=2)), dt.timezone(dt.timedelta(hours=3))
        self.srv.ops = [op('QR9001', 'ZRH', 'DOH', '2031-10-26 02:50', '2031-10-26 10:30', ddelay=20,
                           sdep=dt.datetime(2031, 10, 26, 2, 50, tzinfo=cest),      # fixed offsets: + minutes = real
                           sarr=dt.datetime(2031, 10, 26, 10, 30, tzinfo=ast))]
        self.check('2031-10-26T02:20:00+02:00')
        self.assertEqual(len(self.notes()), 1, self.srv.requests)
        self.assertIn('departure 02:10 instead of 02:50 (+20 min)', self.note())



PHONE_STUB = """#!/bin/sh
echo "$*" >> "$FAKE_PHONE_LOG"
rc=$(cat "$FAKE_PHONE_RC" 2>/dev/null || echo 0)
if [ "$rc" = 4 ]; then echo '{"ok": false, "status": "no_device"}'; else echo '{"ok": true, "status": "sent"}'; fi
exit $rc
"""


class Live(FW):
    """The phone app's Live Update chip (livecount) with a stub phone: 3 h before the (estimated) departure, gate as
    the detail, updated on delay or a gate change, ended at departure or when cancelled."""
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

    def test_chip_3h_before_updates_on_delay_and_gate_ends_at_departure(self):
        self.sync()
        self.srv.ops = [leg1(gate='B12', terminal='1'), leg2()]
        self.check('2026-11-14T12:00:00+01:00')
        self.assertEqual(self.calls(), [], '4 h 10 min ahead')
        self.check('2026-11-14T13:20:00+01:00')
        c = self.calls()
        self.assertEqual(len(c), 1, c)
        self.assertTrue(c[0].startswith('push --level info --title Flight 16:10 --body terminal 1 · gate B12 '
                                        '--tag live:flight:'), c)
        self.assertIn('--live countdown:%d' % at('FRA', '2026-11-14 16:10').timestamp(), c[0])
        self.assertNotIn('QR', c[0])
        self.srv.ops = [leg1(gate='A7', terminal='1', ddelay=20), leg2()]
        self.check('2026-11-14T13:35:00+01:00')
        c = self.calls()
        self.assertEqual(len(c), 2, c)
        self.assertIn('--title Flight 16:10 +20 min --body terminal 1 · gate A7', c[1])
        self.assertIn('--live countdown:%d' % at('FRA', '2026-11-14 16:30').timestamp(), c[1])
        self.check('2026-11-14T13:40:00+01:00')            # not due: the fast path keeps the chip as it is
        self.assertEqual(len(self.calls()), 2)
        self.srv.ops = [leg1(gate='A7', terminal='1', ddelay=20, departed=True), leg2()]
        self.check('2026-11-14T16:33:00+01:00')
        c = self.calls()
        self.assertEqual(len(c), 3, c)
        self.assertTrue(c[2].startswith('cancel live:flight:'), c)

    def test_cancelled_flight_ends_the_chip(self):
        self.sync()
        self.srv.ops = [leg1(), leg2()]
        self.check('2026-11-14T13:20:00+01:00')
        self.assertIn('--body gate not out yet', self.calls()[0])
        self.srv.ops = [leg1(cancelled=True), leg2()]
        self.check('2026-11-14T13:35:00+01:00')
        c = self.calls()
        self.assertEqual(len(c), 2, c)
        self.assertTrue(c[1].startswith('cancel live:flight:'), c)

    def test_no_stub_no_phone(self):
        self.env.pop('LIVECOUNT_PHONE')
        self.sync()
        self.srv.ops = [leg1(), leg2()]
        self.check('2026-11-14T13:20:00+01:00')
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, 'live')))


for _n in [n for n in dir(FW) if n.startswith('test')]:
    setattr(Live, _n, None)                       # Live reuses FW's fixtures, not its tests


if __name__ == '__main__':
    unittest.main(verbosity=1)
