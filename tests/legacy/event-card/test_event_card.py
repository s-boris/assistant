"""Offline tests for event-card: a loopback mock Bot API, fake train-watch / gohome / vault commands, a fictional
profile, calendar and forecast. No real sends. EVENT_CARD_BIN=/path/to/copy to test a copy."""
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.environ.get('EVENT_CARD_BIN') or os.path.expanduser('~/bin/event-card')
TMP = tempfile.mkdtemp(prefix='event-card-tests-')
FAKE = os.path.join(TMP, 'fake')
os.makedirs(FAKE)
CALLS, ALLM, PINNED = [], [], {1}          # message 1 = the status board tg-status pinned earlier
NEXT_ID = [100]
FAIL_EDIT = [False]
FAILS = []


class Bot(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        method = self.path.rsplit('/', 1)[-1]
        p = json.loads(self.rfile.read(int(self.headers['Content-Length'])) or b'{}')
        CALLS.append((method, p))
        ALLM.append((method, 'message_id' in p))
        res = True
        if method == 'sendMessage':
            NEXT_ID[0] += 1
            res = {'message_id': NEXT_ID[0]}
        elif method == 'pinChatMessage':
            PINNED.add(p['message_id'])
        elif method == 'unpinChatMessage':
            PINNED.discard(p['message_id'] if 'message_id' in p else max(PINNED))
        elif method == 'editMessageText' and FAIL_EDIT[0]:
            body = {'ok': False, 'error_code': 400, 'description': 'Bad Request: message to edit not found'}
            return self.reply(400, body)
        self.reply(200, {'ok': True, 'result': res})

    def reply(self, code, body):
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)


srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Bot)
threading.Thread(target=srv.serve_forever, daemon=True).start()
API = 'http://127.0.0.1:%d' % srv.server_address[1]


def write(name, obj):
    path = os.path.join(TMP, name)
    with open(path, 'w') as f:
        json.dump(obj, f)
    return path


def fake(name, body):
    path = os.path.join(FAKE, name)
    with open(path, 'w') as f:
        f.write('#!/bin/sh\n' + body + '\n')
    os.chmod(path, 0o755)
    return path


PROFILE = write('profile.json', {'home': {'street': 'Musterweg 7', 'postcode': '9999', 'town': 'Testdorf',
                                          'town_aliases': ['Testdorf Bahnhof']},
                                 'work': {'town': 'Betaburg'}, 'locale': {'tz': 'Europe/Zurich'}})
D = '2031-03-06'


def ev(eid, title, start, end, town, travel=None, train=None):
    e = {'id': eid, 'start': start + ':00+01:00', 'end': end + ':00+01:00', 'all_day': False, 'title': title,
         'town': town, 'free': True, 'flags': {'travel': travel}}
    if train:
        e['train'] = train
    return e


GIG = [ev('out1', '🚆 Home → Alpha HB', f'{D}T18:14', f'{D}T19:23', 'Testdorf', 'train',
          {'from': 'home', 'to': 'Alpha HB', 'lines': ['S9']}),
       ev('gig1', 'Test Gig', f'{D}T19:00', f'{D}T22:30', 'Alpha'),
       ev('back1', '🚆 Alpha HB → Home', f'{D}T23:07', '2031-03-07T00:14', 'Alpha', 'train',
          {'from': 'Alpha HB', 'to': 'home', 'lines': ['S9']})]
CAL = os.path.join(TMP, 'events.json')
TWDIR = os.path.join(TMP, 'trains')
os.makedirs(TWDIR)
write('trains/legs.json', {'legs': [{'id': 'aaaa1111', 'event_id': 'out1'}, {'id': 'bbbb2222', 'event_id': 'back1'}]})


def calendar(events):
    write('events.json', {'version': 1, 'events': {e['id']: e for e in events}})


def tw(leg, delay=0, cancelled=False, problems=(), plat='3'):
    write(f'fake/tw-{leg}.json', {'id': leg, 'fine': not problems, 'problems': [{'key': k, 'text': k} for k in problems],
                                  'live': {'dep': '18:14', 'arr': '19:23', 'delay': delay, 'cancelled': cancelled,
                                           'lines': ['S9'], 'platform': plat, 'changes': 0, 'text': '18:14 S9 → 19:23'},
                                  'alt': {'text': '18:44 S9 → 19:53', 'link': 'https://example.invalid/alt'}
                                  if problems else None})


def tw_state(leg, last, noted=()):
    write('trains/state.json', {'legs': {leg: {'noted': list(noted), 'last': last}}})


def place(kind):
    write('fake/place.json', {'place': {'kind': kind, 'town': None}})


def conn(dep, arr, lines, plat):
    return {'dep': dep + ':00+01:00', 'arr': arr + ':00+01:00', 'changes': 0, 'stop': 'Alpha HB',
            'platform': plat, 'lines': lines, 'wait': None, 'link': 'https://example.invalid/c'}


write('fake/gohome.json', {'home': False, 'origin': 'Alpha HB', 'next': [
    conn(f'{D}T22:37', f'{D}T23:44', ['S9'], '41'), conn(f'{D}T23:05', f'{D}T23:58', ['RE48', 'Bus 21'], '11'),
    conn(f'{D}T23:07', '2031-03-07T00:14', ['S9'], '41')], 'last': conn('2031-03-07T00:08', '2031-03-07T01:14', ['S9'], '41')})
hours = [f'{D}T{h:02d}:00' for h in range(24)] + [f'2031-03-07T{h:02d}:00' for h in range(24)]
WEATHER = write('weather.json', {'hourly': {'time': hours, 'temperature_2m': [9.0 - i * 0.1 for i in range(48)],
                                            'precipitation_probability': [40] * 48, 'weather_code': [61] * 48}})
fake('train-watch', 'echo "$*" >> "%s/tw.log"; cat "%s/tw-$2.json"' % (FAKE, FAKE))
fake('gohome', 'echo "$*" >> "%s/gh.log"; cat "%s/gohome.json"' % (FAKE, FAKE))
fake('vault', 'cat "%s/place.json"' % FAKE)
fake('rave-radar', 'echo \'[{"name": "Testo Brejchin", "aliases": []}]\'')
fake('trackid', 'echo "$*" >> "%s/tid.log"; cat "%s/ids.json"' % (FAKE, FAKE))
fake('lists', 'cat "%s/crate.json"' % FAKE)
write('fake/ids.json', [])
write('fake/crate.json', {'list': 'crate', 'open': [], 'done': []})
STATE = os.path.join(TMP, 'state')
BUTTONS = os.path.join(TMP, 'buttons.json')
os.makedirs(os.path.join(TMP, 'home'))


def run(cmd, now, *extra, **env):
    e = {**os.environ, 'EVENT_CARD_DIR': STATE, 'EVENT_CARD_NOW': now, 'EVENT_CARD_API': API,
         'EVENT_CARD_TEST_TOKEN': '123456:TESTTOKEN', 'EVENT_CARD_TEST_CHAT': '42', 'EVENT_CARD_CALCACHE': CAL,
         'EVENT_CARD_TW_DIR': TWDIR, 'EVENT_CARD_TRAIN_WATCH': os.path.join(FAKE, 'train-watch'),
         'EVENT_CARD_GOHOME': os.path.join(FAKE, 'gohome'), 'EVENT_CARD_VAULT': os.path.join(FAKE, 'vault'),
         'EVENT_CARD_RAVE_RADAR': os.path.join(FAKE, 'rave-radar'), 'EVENT_CARD_WEATHER': WEATHER,
         'EVENT_CARD_GEO': 'off', 'ASSISTANT_PROFILE': PROFILE, 'HOME_PLACE_GEO': 'off',
         'HOME_PLACE_CACHE': os.path.join(TMP, 'hp.json'), 'http_proxy': '', 'https_proxy': '',
         'no_proxy': '127.0.0.1', 'HOME': os.path.join(TMP, 'home'),
         'EVENT_CARD_TRACKID': os.path.join(FAKE, 'trackid'), 'EVENT_CARD_LISTS': os.path.join(FAKE, 'lists'),
         'EVENT_CARD_BUTTONS': BUTTONS, **env}
    p = subprocess.run([BIN, cmd, *extra], capture_output=True, text=True, env=e, timeout=60)
    return p.returncode, p.stdout, p.stderr


def check(name, cond, info=''):
    print(('ok   ' if cond else 'FAIL ') + name + ('' if cond else '  ' + str(info)[:600]))
    if not cond:
        FAILS.append(name)


def methods():
    return [m for m, _ in CALLS]


def state():
    try:
        with open(os.path.join(STATE, 'state.json')) as f:
            return json.load(f)
    except OSError:
        return {}


# ---------------------------------------------------------------- no event, before the window, dry run
rc, out, err = run('run', f'{D}T16:44')
check('no calendar cache: exit 0, no calls', rc == 0 and not CALLS, (rc, err, CALLS))
calendar(GIG)
tw('aaaa1111')
tw('bbbb2222', plat='41')
tw_state('aaaa1111', f'{D}T16:41:00+01:00')
place('home')
rc, out, err = run('run', f'{D}T16:43')
check('before first leg − 90 min: no calls', rc == 0 and not CALLS, (rc, err, CALLS))
rc, out, err = run('run', f'{D}T16:44', '--dry-run')
check('dry run: card printed, no calls, no state', rc == 0 and 'Leave by 18:14' in out and not CALLS
      and not os.path.exists(os.path.join(STATE, 'state.json')), (rc, out, err, CALLS))

# ---------------------------------------------------------------- start: one silent send + silent pin
rc, out, err = run('run', f'{D}T16:44')
sends = [p for m, p in CALLS if m == 'sendMessage']
pins = [p for m, p in CALLS if m == 'pinChatMessage']
check('start: one send + one pin', rc == 0 and methods() == ['sendMessage', 'pinChatMessage'], (rc, err, CALLS))
check('start: send is silent', sends and sends[0].get('disable_notification') is True, sends)
check('start: pin is silent and by id', pins and pins[0].get('disable_notification') is True
      and pins[0].get('message_id') == 101, pins)
card = sends[0]['text'] if sends else ''
TALK_KB = {'inline_keyboard': [[{'text': '📞 Talk', 'callback_data': 'call:talk'}]]}
check('start: the card carries the 📞 Talk button (call:talk)', sends and sends[0].get('reply_markup') == TALK_KB, sends)
bj0 = json.load(open(BUTTONS)) if os.path.exists(BUTTONS) else {}
check('start: the card is marked "keep" in buttons.json', (bj0.get('42:101') or {}).get('on_press') == 'keep', bj0)
check('card: line 1 is the header with leave time', card.split('\n')[0].startswith('<b>🎟 Test Gig · 🚶 leave 18:14'),
      card)
check('card: next leg with platform + leave by + weather', 'S9 home → Alpha HB · pl 3 · on time' in card
      and 'Leave by 18:14 (in 1 h 30)' in card and 'Alpha 18–22 h' in card and 'rain 40 %' in card, card)
check('state: message id kept', state().get('message_id') == 101, state())

# ---------------------------------------------------------------- next runs edit only
CALLS.clear()
place('away')
rc, out, err = run('run', f'{D}T16:46')
check('second run: one silent edit, no send/pin', methods() == ['editMessageText']
      and CALLS[0][1]['message_id'] == 101 and 'disable_notification' not in CALLS[0][1], CALLS)
CALLS.clear()
rc, out, err = run('run', f'{D}T21:40')
txt = CALLS[0][1]['text'] if CALLS else ''
check('after the start: way home block (3 + last), leg home next', 'Home from Alpha HB' in txt and '22:37' in txt
      and '23:05' in txt and '🌙 Last' in txt and 'Next: 23:07 S9 Alpha HB → home · pl 41' in txt
      and 'Leave for Alpha HB by 22:57' in txt, txt)
check('after the start: header = way home', txt.startswith('<b>🎟 Test Gig · 🏠 next 22:37 · last 00:08'), txt)
check('gohome asked from the leg home start stop', open(os.path.join(FAKE, 'gh.log')).read().startswith('Alpha HB'))

# ---------------------------------------------------------------- delay ≥ 10: train-watch covers it vs. not
CALLS.clear()
tw('bbbb2222', delay=12, problems=['delay15'], plat='41')
tw_state('bbbb2222', f'{D}T22:31:00+01:00')
rc, out, err = run('run', f'{D}T22:40')
txt = CALLS[0][1]['text'] if CALLS else ''
check('delay 12 + train-watch fresh: red header, no extra message', methods() == ['editMessageText']
      and txt.startswith('<b>🔴 Test Gig') and '+12 min' in txt, (CALLS, err))
CALLS.clear()
tw_state('bbbb2222', f'{D}T21:00:00+01:00', noted=['delay15'])
rc, out, err = run('run', f'{D}T22:42')
check('delay noted by train-watch: no extra message', methods() == ['editMessageText'], CALLS)
check('live edits keep the Talk button', CALLS and CALLS[0][1].get('reply_markup') == TALK_KB, CALLS)
CALLS.clear()
tw_state('bbbb2222', f'{D}T21:00:00+01:00')
rc, out, err = run('run', f'{D}T22:44')
alerts = [p for m, p in CALLS if m == 'sendMessage']
check('delay + train-watch stale: ONE normal message (notifies)', len(alerts) == 1
      and 'disable_notification' not in alerts[0] and '🔴' in alerts[0]['text'], CALLS)
CALLS.clear()
rc, out, err = run('run', f'{D}T22:46')
check('no second alert for the same leg', methods() == ['editMessageText'], CALLS)

# ---------------------------------------------------------------- the card vanished: resend + pin
CALLS.clear()
FAIL_EDIT[0] = True
rc, out, err = run('run', f'{D}T22:48')
FAIL_EDIT[0] = False
check('edit not found: new card sent + pinned', methods()[-2:] == ['sendMessage', 'pinChatMessage']
      and state().get('message_id') == 103, (CALLS, state()))

# ---------------------------------------------------------------- he stays past the leg home: never "on the way"
def at_venue(activity='still', town='Alpha'):
    write('fake/place.json', {'place': {'kind': 'away', 'town': town, 'band': 'region', 'fix_age_min': 3},
                              'drive': {'active': False}, 'activity': activity})


GH0 = open(os.path.join(FAKE, 'gohome.json')).read()
CALLS.clear()
tw('bbbb2222', plat='41')
at_venue()
rc, out, err = run('run', f'{D}T23:10')
check('stays, inside the 5-min grace: not marked missed yet', not state().get('missed'), state())
S9W = dict(conn('2031-03-07T00:08', '2031-03-07T01:14', ['S9'], '41'),   # gohome's last mile: 17 min on foot
           off='Omega', off_arr='2031-03-07T00:57:00+01:00', walk_min=17)
write('fake/gohome.json', {'home': False, 'origin': 'Alpha HB', 'next': [
    conn(f'{D}T23:37', '2031-03-07T00:44', ['S12'], '43'), S9W], 'last': S9W})
CALLS.clear()
rc, out, err = run('run', f'{D}T23:20')
txt = CALLS[0][1]['text'] if CALLS else ''
check('stays past the leg home: header = way home, leg marked left, never "on the way"',
      txt.startswith('<b>🎟 Test Gig · 🏠 next 23:37 · last 00:08') and 'Planned 23:07 S9 left without you' in txt
      and 'On the way' not in txt and state().get('missed'), (txt, state()))
check('a long last mile is named: last stop + its arrival, then home on foot; a short one stays "→ HH:MM"',
      'Omega 00:57, 🚶 home 01:14' in txt and '→ 00:44' in txt, txt)
check('missed leg recorded by its train-watch id', list(state().get('missed_legs') or {}), ['bbbb2222'])
rc, out, err = run('status', f'{D}T23:21', '--json')
check('status --json lists the missed leg (train-watch notes on it are moot)',
      list((json.loads(out or '{}').get('missed_legs') or {})), ['bbbb2222'])
CALLS.clear()
at_venue('vehicle', None)
rc, out, err = run('run', f'{D}T23:45')
txt = CALLS[0][1]['text'] if CALLS else ''
check('missed, then moving: the likely train since he was last seen there (23:37 S12), no way-home block',
      txt.startswith('<b>🎟 Test Gig · 🚆 S12 → home 00:44') and 'On the way (likely): S12 23:37 from Alpha HB' in txt
      and 'Home from' not in txt, txt)
CALLS.clear()
at_venue(town=None)
rc, out, err = run('run', f'{D}T23:47')
txt = CALLS[0][1]['text'] if CALLS else ''
check('missed, place unknown: way home shown again, no guessed train', 'Home from Alpha HB' in txt
      and 'likely' not in txt, txt)
write('fake/gohome.json', json.loads(GH0))

# ---------------------------------------------------------------- still_here: the one drive rule (core.drive)
import importlib.machinery, importlib.util  # noqa: E401,E402
_ld = importlib.machinery.SourceFileLoader('event_card_mod', BIN)
_spec = importlib.util.spec_from_loader('event_card_mod', _ld)
EC = importlib.util.module_from_spec(_spec)
_ld.exec_module(EC)
_venue = {'town': 'Alpha'}


def _at(drive, age):
    return {'place': {'kind': 'away', 'town': 'Alpha', 'band': 'region'}, 'drive': {'active': drive},
            'activity': 'still', 'age_min': age}


check('still_here: a fresh drive flag = moving (False)', EC.still_here(_at(True, 5), _venue) is False)
check('still_here: a drive flag older than 60 min is no drive: still there (True)',
      EC.still_here(_at(True, 90), _venue) is True)
check('still_here: not driving, in the town: True', EC.still_here(_at(False, 5), _venue) is True)

# ---------------------------------------------------------------- home: final edit + unpin by id, record kept
CALLS.clear()
tw('bbbb2222', plat='41')
place('home')
rc, out, err = run('run', f'{D}T23:50')
check('home: final edit + unpin by id, nothing deleted', methods() == ['editMessageText', 'unpinChatMessage']
      and CALLS[1][1].get('message_id') == 103 and '✅ Test Gig · home 23:50' in CALLS[0][1]['text'], CALLS)
check('home: the final edit drops the Talk button', CALLS and 'reply_markup' not in CALLS[0][1], CALLS)
check('home: tg-status board stays pinned', 1 in PINNED and 103 not in PINNED, PINNED)
check('home: state cleared, night done', 'message_id' not in state()
      and any(v.get('why') == 'home' for v in state().get('done', {}).values()), state())
CALLS.clear()
rc, out, err = run('run', f'{D}T23:52')
check('after home: no calls', not CALLS, CALLS)

# ---------------------------------------------------------------- cutoff, off, rave without legs
D2 = '2031-03-13'
calendar([ev('out2', '🚆 Home → Alpha HB', f'{D2}T20:00', f'{D2}T21:00', 'Testdorf', 'train',
             {'from': 'home', 'to': 'Alpha HB', 'lines': ['S9']}),
          ev('gig2', 'Late Show', f'{D2}T21:30', f'{D2}T23:30', 'Alpha')])
place('away')
CALLS.clear()
run('run', f'{D2}T18:30', EVENT_CARD_TALK='off')
check('second night starts', methods() == ['sendMessage', 'pinChatMessage'], CALLS)
check('EVENT_CARD_TALK=off: no Talk button, no keep mark', CALLS and 'reply_markup' not in CALLS[0][1]
      and '42:104' not in (json.load(open(BUTTONS)) if os.path.exists(BUTTONS) else {}), CALLS)
CALLS.clear()
run('run', '2031-03-14T04:45')
check('cutoff 04:45: final edit + unpin by id', methods() == ['editMessageText', 'unpinChatMessage']
      and 'live card ended' in CALLS[0][1]['text'] and CALLS[1][1].get('message_id') == 104, CALLS)

D3 = '2031-03-20'
calendar([ev('rave3', 'Techno Night', f'{D3}T23:00', '2031-03-21T05:00', 'Betaburg'),
          ev('dinner3', 'Dinner', f'{D3}T19:00', f'{D3}T21:00', 'Alpha')])
CALLS.clear()
run('run', f'{D3}T21:29')
check('rave without legs: nothing before start − 90 min', not CALLS, CALLS)
run('run', f'{D3}T21:30')
check('rave without legs: card at start − 90 min (dinner without legs ignored)',
      methods() == ['sendMessage', 'pinChatMessage'] and 'Techno Night' in CALLS[0][1]['text'], CALLS)
CALLS.clear()
rc, out, err = run('off', f'{D3}T21:40')
check('off: final edit + unpin by id', methods() == ['editMessageText', 'unpinChatMessage']
      and CALLS[1][1].get('message_id') == 105, CALLS)
CALLS.clear()
run('run', f'{D3}T21:42')
check('off: no new card tonight', not CALLS, CALLS)

D4 = '2031-03-27'
calendar([ev('art4', 'Testo Brejchinn', f'{D4}T22:00', '2031-03-28T04:00', 'Betaburg')])
os.remove(os.path.join(STATE, 'artists.json'))
CALLS.clear()
run('run', f'{D4}T20:30')
check('rave-radar artist (typo) counts as an event night', methods() == ['sendMessage', 'pinChatMessage'], CALLS)

# ---------------------------------------------------------------- global
check('never unpinAll / delete; every unpin names its message', not any(m in ('unpinAllChatMessages', 'deleteMessage') for m, _ in ALLM)
      and all(has for m, has in ALLM if m == 'unpinChatMessage'), ALLM)
rc, out, err = run('status', f'{D4}T20:31')
check('status lists the live card', rc == 0 and 'message 106' in out, (out, err))
st_mode = oct(os.stat(os.path.join(STATE, 'state.json')).st_mode & 0o777)
check('state file 0600', st_mode == '0o600', st_mode)

# ---------------------------------------------------------------- night recap (trackid hits + ➕ Crate buttons)
D5, D5b = '2032-01-15', '2032-01-16'
calendar([ev('out5', '🚆 Home → Alpha HB', f'{D5}T20:00', f'{D5}T21:00', 'Testdorf', 'train',
             {'from': 'home', 'to': 'Alpha HB', 'lines': ['S9']}),
          ev('rave5', 'Recap Rave', f'{D5}T22:00', f'{D5b}T03:00', 'Alpha'),
          ev('back5', '🚆 Alpha HB → Home', f'{D5b}T02:10', f'{D5b}T03:05', 'Alpha', 'train',
             {'from': 'Alpha HB', 'to': 'home', 'lines': ['S9']})])
write('fake/gohome.json', {'home': False, 'origin': 'Alpha HB', 'next': [
    conn(f'{D5b}T01:12', f'{D5b}T02:05', ['IR36', 'S9'], '7'), conn(f'{D5b}T02:10', f'{D5b}T03:05', ['S9'], '41')],
    'last': conn(f'{D5b}T02:10', f'{D5b}T03:05', ['S9'], '41')})


def hit(tid, ts, artist, title, url=None):
    return {'id': tid, 'crate': f'{artist} – {title}', 'artist': artist, 'title': title, 'ts': ts + ':00+01:00',
            'count': 1, 'deezer_url': url, 'shazam_url': None}


HITS = [hit('tid-0ld00004', f'{D5}T12:00', 'Day Act', 'Lunch Tune'),
        hit('tid-aaaa0001', f'{D5}T23:41', 'Testo Brejchin', 'Gravity Test', 'https://example.invalid/d?a=1&b=2'),
        hit('tid-aaaa0002', f'{D5b}T00:15', 'DJ Fake', 'Night Drive'),
        hit('tid-aaaa0003', f'{D5b}T01:02', 'Artist <x>', 'A&B Very Long Title For A Button Label')]
write('fake/ids.json', HITS)
write('fake/crate.json', {'list': 'crate', 'open': [{'n': 1, 'text': 'dj fake - NIGHT DRIVE'}], 'done': []})
place('away')
CALLS.clear()
run('run', f'{D5}T18:30')
check('the card of a night no longer in the calendar ends as gone (edit + unpin)',
      methods() == ['editMessageText', 'unpinChatMessage'] and 'recap' not in state(), CALLS)
CALLS.clear()
run('run', f'{D5}T18:32')
mid5 = NEXT_ID[0]
check('recap night: card sent', methods() == ['sendMessage', 'pinChatMessage'], CALLS)
run('run', f'{D5}T23:30')
check('recap night: connections home remembered', any('01:12' in k for k in state().get('conns', {})), state())
place('home')
CALLS.clear()
rc_, out, err = run('run', f'{D5b}T02:12', '--dry-run')
check('dry run: recap printed, no calls, no state change', rc_ == 0 and not CALLS and 'night recap (3 tracks' in out
      and '[➕ 1. Gravity Test] -> rr:crate:tid-aaaa0001' in out and state().get('message_id') == mid5, (out, err))
if os.environ.get('SHOW_RECAP'):
    print(out)
a0 = len(ALLM)
run('run', f'{D5b}T02:12')
edits = [p for m, p in CALLS if m == 'editMessageText']
txt = edits[0]['text'] if edits else ''
kb = [r[0]['callback_data'] for r in (edits[0].get('reply_markup') or {}).get('inline_keyboard', [])] if edits else []
check('home with IDs: ONE edit into the recap, stays pinned, no send', methods() == ['editMessageText']
      and edits[0]['message_id'] == mid5 and mid5 in PINNED, CALLS)
check('recap: header line = event + count', txt.split('\n')[0] == '<b>🌙 Recap Rave · 3 tracks IDed</b>', txt)
check('recap: way home = the connection that got him home', '🏠 Home 02:12 · 🚆 IR36 S9 01:12 from Alpha HB → 02:05'
      in txt, txt)
check('recap: tracks in order with times, links, escaping, crate mark',
      '1. 23:41 <a href="https://example.invalid/d?a=1&amp;b=2">Testo Brejchin – Gravity Test</a>' in txt
      and '2. 00:15 DJ Fake – Night Drive · ✓ crate' in txt and '3. 01:02 Artist &lt;x&gt; – A&amp;B' in txt
      and 'Lunch Tune' not in txt, txt)
check('recap: ➕ buttons only for tracks not in the crate', kb == ['rr:crate:tid-aaaa0001', 'rr:crate:tid-aaaa0003'], kb)
labels = [r[0]['text'] for r in edits[0]['reply_markup']['inline_keyboard']] if edits else []
check('recap: button labels short', all(len(x) <= 32 for x in labels) and labels[0] == '➕ 1. Gravity Test', labels)
bj = json.load(open(BUTTONS)) if os.path.exists(BUTTONS) else {}
check('recap: keyboard marked keep for telegram-plus', (bj.get(f'42:{mid5}') or {}).get('on_press') == 'keep', bj)
st5 = state()
check('recap: state = recap open, card cleared, night done', 'message_id' not in st5 and 'conns' not in st5
      and (st5.get('recap') or {}).get('pinned') is True and any(k.startswith('rave5@') for k in st5['done']), st5)
CALLS.clear()
run('run', f'{D5b}T02:14')
check('recap: unchanged → no calls', not CALLS, CALLS)
write('fake/crate.json', {'list': 'crate', 'open': [{'n': 1, 'text': 'DJ Fake – Night Drive'}],
                          'done': [{'n': 2, 'text': 'Testo Brejchin – Gravity Test'}]})
run('run', f'{D5b}T02:16')
kb = [r[0]['callback_data'] for p in [CALLS[0][1]] for r in (p.get('reply_markup') or {}).get('inline_keyboard', [])] \
    if CALLS else []
check('recap: a track reaching the crate loses its button (one edit)', methods() == ['editMessageText']
      and kb == ['rr:crate:tid-aaaa0003'] and '1. 23:41' in CALLS[0][1]['text'], CALLS)
write('fake/ids.json', HITS + [hit('tid-aaaa0005', f'{D5b}T10:30', 'Morning Act', 'Clip From Last Night')])
CALLS.clear()
run('run', f'{D5b}T10:32')
check('recap: a clip IDed in the morning joins (edit only)', methods() == ['editMessageText']
      and '4 tracks IDed' in CALLS[0][1]['text'] and 'Clip From Last Night' in CALLS[0][1]['text'], CALLS)
check('recap phase never sends a new (notifying) message', all(m in ('editMessageText', 'unpinChatMessage')
                                                               for m, _ in ALLM[a0:]), ALLM[a0:])
rc_, out, err = run('status', f'{D5b}T10:33', '--json')
check('status shows the open recap', json.loads(out).get('recap', {}).get('message_id') == mid5, (out, err))
CALLS.clear()
run('run', f'{D5b}T18:00')
check('recap ends 18:00 the morning after: unpin by id only', methods() == ['unpinChatMessage']
      and CALLS[0][1].get('message_id') == mid5 and mid5 not in PINNED and 1 in PINNED, CALLS)
check('recap ended: gone from state', 'recap' not in state(), state())
CALLS.clear()
run('run', f'{D5b}T18:02')
check('after the recap: no calls', not CALLS, CALLS)

# no IDs at the end: unpin as before; a clip IDed later edits the unpinned record (no pin, no send)
D6, D6b = '2032-01-22', '2032-01-23'
calendar([ev('rave6', 'Techno Night Six', f'{D6}T23:00', f'{D6b}T05:00', 'Betaburg')])
write('fake/ids.json', HITS)
write('fake/crate.json', {'list': 'crate', 'open': [], 'done': []})
place('away')
run('run', f'{D6}T21:30')
mid6 = NEXT_ID[0]
CALLS.clear()
run('run', f'{D6b}T04:46')
check('cutoff without IDs: final edit + unpin as before', methods() == ['editMessageText', 'unpinChatMessage']
      and 'reply_markup' not in CALLS[0][1] and mid6 not in PINNED, CALLS)
check('rave-only night past 04:45 ends as cutoff (titled), not gone',
      CALLS and CALLS[0][1]['text'].startswith('<b>🌙 Techno Night Six · live card ended 04:46'), CALLS)
write('fake/ids.json', HITS + [hit('tid-bbbb0006', f'{D6b}T09:10', 'Late Clip', 'Found It')])
CALLS.clear()
run('run', f'{D6b}T09:12')
txt = CALLS[0][1]['text'] if CALLS else ''
check('late ID: the record becomes the recap, edit only, not re-pinned', methods() == ['editMessageText']
      and txt.startswith('<b>🌙 Techno Night Six · 1 track IDed</b>') and '🏠' not in txt and mid6 not in PINNED
      and [r[0]['callback_data'] for r in CALLS[0][1]['reply_markup']['inline_keyboard']] == ['rr:crate:tid-bbbb0006'],
      CALLS)
CALLS.clear()
run('run', f'{D6b}T18:00')
check('unpinned recap ends without calls', not CALLS and 'recap' not in state(), (CALLS, state()))

# a pinned recap makes way for the next night's card; trackid down = no recap edit
D8, D8b = '2032-01-29', '2032-01-30'
calendar([ev('rave8', 'Techno Night Eight', f'{D8}T23:00', f'{D8b}T04:00', 'Betaburg'),
          ev('out9', '🚆 Home → Alpha HB', f'{D8b}T16:00', f'{D8b}T17:00', 'Testdorf', 'train',
             {'from': 'home', 'to': 'Alpha HB', 'lines': ['S9']}),
          ev('gig9', 'Next Day Gig', f'{D8b}T17:30', f'{D8b}T20:00', 'Alpha')])
write('fake/ids.json', [hit('tid-cccc0008', f'{D8b}T01:00', 'Eight', 'Tune')])
place('away')
run('run', f'{D8}T21:30')
mid8 = NEXT_ID[0]
place('home')
CALLS.clear()
run('run', f'{D8b}T03:20')
check('night 8: recap pinned', methods() == ['editMessageText'] and mid8 in PINNED, CALLS)
fake('trackid', 'exit 3')
CALLS.clear()
run('run', f'{D8b}T09:00')
check('trackid failing: recap left as it is', not CALLS and state().get('recap'), (CALLS, state()))
place('away')
run('run', f'{D8b}T14:30')
check('next card due: recap unpinned by id first, then send + pin', methods() == ['unpinChatMessage', 'sendMessage',
                                                                                 'pinChatMessage']
      and CALLS[0][1].get('message_id') == mid8 and mid8 not in PINNED and 'recap' not in state(), CALLS)
fake('trackid', 'echo "$*" >> "%s/tid.log"; cat "%s/ids.json"' % (FAKE, FAKE))
check('trackid asked with list 200 --json', open(os.path.join(FAKE, 'tid.log')).readline().strip() == 'list 200 --json')
check('recap tests: no unpinAll / delete; every unpin by id',
      not any(m in ('unpinAllChatMessages', 'deleteMessage') for m, _ in ALLM)
      and all(has for m, has in ALLM if m == 'unpinChatMessage'), ALLM)
# ---------------------------------------------------------------- the autumn clock change (CEST -> CET at 03:00)
# 02:00-02:59 runs twice: a leg that left at 02:08 CEST must not come back as "Next" at 02:00 CET, and the ride
# (02:08 CEST -> 02:16 CET, 68 real minutes) stays "on the way" through the repeated hour.
STATE = os.path.join(TMP, 'state-dst')
DZ1, DZ2 = '2031-10-25', '2031-10-26'                     # 26.10.2031 = the last Sunday of October


def evz(eid, title, start, end, town, travel=None, train=None):
    e = {'id': eid, 'start': start, 'end': end, 'all_day': False, 'title': title, 'town': town, 'free': True,
         'flags': {'travel': travel}}
    if train:
        e['train'] = train
    return e


calendar([evz('dstparty', 'Late Party', f'{DZ1}T20:00:00+02:00', f'{DZ2}T02:00:00+02:00', 'Alpha'),
          evz('dstback', '🚆 Alpha HB → Home', f'{DZ2}T02:08:00+02:00', f'{DZ2}T02:16:00+01:00', 'Alpha', 'train',
              {'from': 'Alpha HB', 'to': 'home', 'lines': ['SN9']})])
write('trains/legs.json', {'legs': [{'id': 'dddd0001', 'event_id': 'dstback'}]})
tw('dddd0001', plat='41')
write('trains/state.json', {'legs': {}})
at_venue()
CALLS.clear()
run('run', f'{DZ2}T01:00:00+02:00')
check('DST: card at 01:00 CEST (leg home 02:08 CEST)', methods() == ['sendMessage', 'pinChatMessage']
      and 'Next: 02:08 S9' in CALLS[0][1]['text'], CALLS)
at_venue('vehicle', None)
for label in ('02:15:00+02:00', '02:30:00+02:00', '02:45:00+02:00', '02:00:00+01:00', '02:12:00+01:00'):
    CALLS.clear()
    rc, out, err = run('run', f'{DZ2}T{label}')
    txt = CALLS[0][1]['text'] if CALLS else ''
    check(f'DST: {label} on the 02:08 CEST -> 02:16 CET ride: "on the way", arrives 02:16, never "Next: 02:08"',
          'On the way: S9 Alpha HB → home, arrives 02:16' in txt and 'Next: 02:08' not in txt
          and 'Leave for' not in txt, (txt, err[-300:]))
CALLS.clear()
place('home')
run('run', f'{DZ2}T02:20:00+01:00')
check('DST: home at 02:20 CET: final edit "home 02:20" + unpin', methods() == ['editMessageText', 'unpinChatMessage']
      and '✅ Late Party · home 02:20' in CALLS[0][1]['text'], CALLS)

# a rave-only night across the change: the 04:45 cutoff is 04:45 CET (wall time), not 04:45 CEST (= 03:45 CET)
calendar([evz('dstrave', 'Techno Night DST', f'{DZ1}T23:00:00+02:00', f'{DZ2}T05:00:00+01:00', 'Betaburg')])
place('away')
CALLS.clear()
run('run', f'{DZ1}T21:30:00+02:00')
check('DST rave: card at start - 90 min', methods() == ['sendMessage', 'pinChatMessage'], CALLS)
CALLS.clear()
run('run', f'{DZ2}T03:50:00+01:00')
check('DST rave: still live at 03:50 CET (past 04:45 CEST)', methods() == ['editMessageText']
      and 'live card ended' not in (CALLS[0][1]['text'] if CALLS else ''), CALLS)
CALLS.clear()
run('run', f'{DZ2}T04:46:00+01:00')
check('DST rave: ends at the 04:45 CET cutoff', methods() == ['editMessageText', 'unpinChatMessage']
      and 'live card ended 04:46' in CALLS[0][1]['text'], CALLS)

shutil.rmtree(TMP, ignore_errors=True)
srv.shutdown()
print('%d failed' % len(FAILS) if FAILS else 'all passed')
sys.exit(1 if FAILS else 0)
