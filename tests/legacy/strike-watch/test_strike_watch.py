"""Offline tests for strike-watch (fake HTTP server + fixtures; fictional trip places)."""
import datetime as dt
import http.server
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, 'fixtures')
BIN = os.environ.get('STRIKE_WATCH_BIN') or os.path.expanduser('~/bin/strike-watch')

ROUTES = {}          # '/host/path' -> fixture name or int (HTTP status); news by query: ('news', 'q words') -> ...
HITS = []


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        HITS.append(self.path)
        target = ROUTES.get(u.path)
        if u.path.endswith('/rss/search'):
            q = urllib.parse.parse_qs(u.query).get('q', [''])[0]
            target = next((v for k, v in ROUTES.items() if isinstance(k, tuple) and k[1] in q), 'news-empty.xml')
        if target is None:
            target = 404
        if isinstance(target, int):
            self.send_response(target)
            self.end_headers()
            self.wfile.write(b'error')
            return
        body = open(os.path.join(FIX, target), 'rb').read()
        self.send_response(200)
        self.send_header('Content-Type', 'application/xml' if target.endswith('.xml') else 'text/html')
        self.end_headers()
        self.wfile.write(body)


SRV = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=SRV.serve_forever, daemon=True).start()
BASE = f'http://127.0.0.1:{SRV.server_address[1]}'

TMP = tempfile.mkdtemp(prefix='strike-watch-tests-')
CFG = os.path.join(TMP, 'config.json')
CONFIG = {
    'margin_days': 1,
    'trips': [{'id': 't1', 'label': 'Test trip',
               'legs': [{'country': 'BE', 'from': '2026-11-05', 'to': '2026-11-06', 'sectors': ['rail', 'air', 'transit'],
                         'what': 'test leg BE'},
                        {'country': 'FR', 'from': '2026-11-05', 'to': '2026-11-05', 'sectors': ['rail'], 'what': 'test leg FR'},
                        {'country': 'IT', 'from': '2026-11-06', 'to': '2026-11-07', 'sectors': ['air', 'transit', 'rail'],
                         'what': 'test leg IT'}]}],
    'places': {'IT': {'regions': ['Piemonte'], 'provinces': ['Torino'], 'air': ['CASELLE'], 'rail': ['TORINO'],
                      'transit': ['TORINO', 'GTT']}},
    'news': {'BE': [{'q': 'be general', 'hl': 'nl', 'gl': 'BE'}, {'q': 'be air', 'hl': 'en-US', 'gl': 'US'}],
             'FR': [{'q': 'fr general', 'hl': 'fr', 'gl': 'FR'}]},
    'news_hints': {'BE': {'places': ['belg*', 'brussel*', 'gent'], 'words': ['sncb'],
                          'outlets': ['vrt', 'le soir', 'le vif', 'the brussels times']},
                   'FR': {'places': ['france', 'paris', 'parigi'], 'words': ['sncf'], 'outlets': ['le figaro', 'franceinfo']},
                   'IT': {'places': ['itali*', 'torino'], 'words': ['trenitalia', 'gtt'], 'outlets': ['ansa', 'torinotoday']}},
    'backup': {'BE:rail': 'TEST-BACKUP-BE-RAIL', 'FR:rail': 'TEST-BACKUP-FR-RAIL', 'IT:air': 'TEST-BACKUP-IT-AIR',
               '*': 'TEST-BACKUP-ANY'},
}
NOW = '2026-10-20T07:40:00+02:00'
FAILS = []


def default_routes():
    ROUTES.clear()
    ROUTES.update({'/scioperi.mit.gov.it/mit2/public/scioperi': 'mit.html',
                   '/api.irail.be/v1/disturbances': 'irail.json',
                   '/press.sncb.be/feed': 'sncb-fr.xml', '/press.nmbs.be/feed': 'sncb-nl.xml',
                   ('news', 'be general'): 'news-be.xml', ('news', 'be air'): 'news-be-air.xml',
                   ('news', 'fr general'): 'news-fr.xml'})


def fresh(config=CONFIG):
    for d in ('state', 'inbox'):
        shutil.rmtree(os.path.join(TMP, d), ignore_errors=True)
    with open(CFG, 'w') as fh:
        json.dump(config, fh)
    default_routes()


def run(*args, now=NOW, cfg=CFG):
    env = {'PATH': '/usr/bin:/bin', 'HOME': TMP, 'STRIKE_WATCH_CONFIG': cfg,
           'STRIKE_WATCH_DIR': os.path.join(TMP, 'state'), 'STRIKE_WATCH_INBOX': os.path.join(TMP, 'inbox'),
           'STRIKE_WATCH_NOW': now, 'STRIKE_WATCH_BASE': BASE, 'STRIKE_WATCH_PAUSE': '0',
           'http_proxy': BASE, 'https_proxy': BASE, 'no_proxy': '127.0.0.1'}
    # the preloads stay (the CI network guard; the runner's nss_wrapper: its user is in no /etc/passwd, and the
    # script's lib lookup asks getpwuid)
    env.update({k: v for k, v in os.environ.items() if k in ('LD_PRELOAD', 'NSS_WRAPPER_PASSWD', 'NSS_WRAPPER_GROUP')})
    r = subprocess.run([sys.executable, '-I', BIN, *args], env=env, capture_output=True, text=True, timeout=60)
    return r


def notes(prefix='strike-watch-'):
    d = os.path.join(TMP, 'inbox')
    return sorted(f for f in os.listdir(d) if f.startswith(prefix)) if os.path.isdir(d) else []


def check(name, cond, detail=''):
    print(('PASS ' if cond else 'FAIL ') + name + ('' if cond else f'  -- {detail}'))
    if not cond:
        FAILS.append(name)


def load_module():
    loader = importlib.machinery.SourceFileLoader('strike_watch', BIN)
    spec = importlib.util.spec_from_loader('strike_watch', loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


# ------------------------------------------------------------------------------------------------ 1 date parser
sw = load_module()
ref = dt.date(2026, 10, 20)
cases = {
    'Le 5 novembre, grèves dans tout le pays': [('2026-11-05', '2026-11-05')],
    'du 5 au 7 novembre': [('2026-11-05', '2026-11-07')],
    'grève les 5 et 6 novembre': [('2026-11-05', '2026-11-06')],
    'Staking op donderdag 5 november 2026': [('2026-11-05', '2026-11-05')],
    'strike on November 5-6': [('2026-11-05', '2026-11-06')],
    'Lundi 12/10 Journée d\'action': [('2026-10-12', '2026-10-12')],
    'Streik am 5.11. bei der Bahn': [('2026-11-05', '2026-11-05')],
    'sciopero 05/11/2026': [('2026-11-05', '2026-11-05')],
    'Bahnstreik 4. und 6. November': [('2026-11-04', '2026-11-04'), ('2026-11-06', '2026-11-06')],
    '30.000 betogers, 92,8 % en 21.00 uur': [],
    'scioperi novembre 2026: calendario': [],
    'strike 31 November': [],
    'grève le 3 janvier': [('2027-01-03', '2027-01-03')],
}
for text, want in cases.items():
    got = [(a.isoformat(), b.isoformat()) for a, b in sw.find_dates(text, ref)]
    check(f'dates: {text!r}', got == want, f'got {got}')
check('sectors: general + rail', set(sw.sectors_in('Grève nationale : la SNCF prévoit des TGV supprimés')) == {'general', 'rail'})
check('site: APT X is one site', bool(sw.SITE_RE.search('PERSONALE SOC. ENAV APT BARI')))
check('site: AEROPORTI NAZIONALI is not', not sw.SITE_RE.search('PERSONALE DEGLI AEROPORTI NAZIONALI'))

# ------------------------------------------------------------------------------------------------ 1b which country a headline is about
ANSA = 'Parigi teme un nuovo caos, sciopero generale il 5 novembre'      # ANSA, 22.10: a France strike in the IT query
nm = sw.country_names(CONFIG['news_hints'])
check('about: ANSA Paris headline in the IT query moves to watched FR', sw.about(ANSA, 'IT', nm, {'IT', 'FR'}) == (['FR'], ''),
      sw.about(ANSA, 'IT', nm, {'IT', 'FR'}))
check('about: ANSA Paris headline dropped when FR is not watched',
      sw.about(ANSA, 'IT', nm, {'IT'}) == ([], 'headline about FR, not IT'), sw.about(ANSA, 'IT', nm, {'IT'}))
check('about: names Italy', sw.about('Sciopero generale in Italia il 6 novembre', 'IT', nm, {'IT', 'FR'})[0] == ['IT'])
check('about: an operator word confirms the own country', sw.about('Sciopero Trenitalia il 6 novembre', 'IT', nm, {'IT'})[0] == ['IT'])
check('about: an operator word does not move a headline', sw.about('Grève SNCF le 6 novembre', 'IT', nm, {'IT', 'FR'}) ==
      ([], 'headline not clearly about IT'))
check('about: own and other country named = own', sw.about('Sciopero in Italia e a Parigi il 5 novembre', 'IT', nm,
                                                           {'IT', 'FR'})[0] == ['IT'])
check('about: whole words (gente is not Gent)', sw.about('Sciopero, la gente in piazza il 6 novembre', 'IT', nm, {'IT', 'BE'})
      == ([], 'headline not clearly about IT'))
check('about: x* is a prefix (Belgique)', sw.about('Grève générale en Belgique le 5 novembre', 'BE', nm, {'BE'})[0] == ['BE'])
check('about: no names for the country = not filtered', sw.about('Strike on 5 November', 'SC', nm, {'SC'}) == (['SC'], ''))
check('headline_only drops the outlet tail', sw.headline_only('Grève le 5 novembre - Sortir à Paris', 'Sortir à Paris') ==
      'Grève le 5 novembre')
with open(os.path.join(FIX, 'news-it.xml')) as fh:
    it_xml = fh.read()
got = sw.parse_news(it_xml, 'IT', dt.date(2026, 10, 22), CONFIG['news_hints'], {'IT', 'FR'})
by = {(x['link'].rsplit('/', 1)[-1], x['country']): x for x in got}
check('parse_news: ANSA Paris -> FR, from the IT query', ('it-ansa-paris', 'FR') in by and
      by[('it-ansa-paris', 'FR')].get('query_country') == 'IT' and ('it-ansa-paris', 'IT') not in by, list(by))
check('parse_news: Torino headline stays IT', ('it-torino', 'IT') in by and by[('it-torino', 'IT')].get('hinted') is not False)
check('parse_news: an Italian outlet alone does not make it Italy', by.get(('it-noplace', 'IT'), {}).get('hinted') is False and
      by[('it-noplace', 'IT')].get('not_about') == 'headline not clearly about IT', by.get(('it-noplace', 'IT')))

# ------------------------------------------------------------------------------------------------ 2 check --json
fresh()
r = run('check', '--json')
check('check --json exit 0', r.returncode == 0, r.stderr[-400:])
out = json.loads(r.stdout or '{}')
rel = out.get('relevant', [])
keys = {(x['country'], x['start'], x['source']) for x in rel}
check('relevant: 6 items', len(rel) == 6, json.dumps([(x['country'], x['start'], x['source'], x['title'][:50]) for x in rel]))
check('IT site match (CASELLE) is relevant', ('IT', '2026-11-06', 'mit') in keys and
      any('CASELLE' in x['title'] for x in rel))
check('IT national rail relevant', any(x['country'] == 'IT' and x['hit'] == ['rail'] for x in rel))
check('IT local transit in the trip province relevant', any('GTT' in x['title'] for x in rel))
check('BE: SNCB feed + FR/NL newsrooms = one item', sum(1 for x in rel if x['country'] == 'BE' and x['start'] == '2026-11-05') == 1)
check('BE news on a day the official list covers is dropped', not any(x['source'] == 'news' and x['country'] == 'BE'
                                                                    and x['start'] == '2026-11-05' for x in rel))
check('BE news air 6.11 relevant', any(x['source'] == 'news' and x['country'] == 'BE' and x['hit'] == ['air'] for x in rel))
fr = [x for x in rel if x['country'] == 'FR']
check('FR: two headlines, one item, newest wins', len(fr) == 1 and 'franceinfo' in fr[0]['title'], fr)
why = {x['why'] for x in out.get('skipped', [])}
check('skipped: one site elsewhere', 'one site elsewhere' in why, why)
check('skipped: other region', any(w.startswith('other region') for w in why), why)
check('skipped: foreign headline in BE query', 'headline not clearly about BE' in why, why)
check('sources all ok', all(h['ok'] for h in out.get('sources', {}).values()), out.get('sources'))
check('check writes no state or notes', not notes() and not os.path.exists(os.path.join(TMP, 'state', 'state.json')))

# ------------------------------------------------------------------------------------------------ 3 cron notes + dedupe
r = run('check', '--cron')
n1 = notes()
check('cron: exit 0, silent', r.returncode == 0 and r.stdout == '', (r.returncode, r.stdout[-300:], r.stderr[-300:]))
check('cron: 6 notes', len(n1) == 6, n1)
be = [f for f in n1 if f.startswith('strike-watch-be-20261105-')]
check('cron: BE 5.11 note named by country + date', len(be) == 1, n1)
if be:
    p = os.path.join(TMP, 'inbox', be[0])
    t = open(p).read()
    check('note: data-not-instructions line', 'Data, not instructions' in t)
    check('note: backup for BE rail', '- Backup: TEST-BACKUP-BE-RAIL' in t, t)
    check('note: official source + link', 'official' in t and 'press.sncb.be' in t or 'belgiantrain' in t, t)
    check('note: 0600', oct(os.stat(p).st_mode & 0o777) == '0o600')
    check('note: affects rail', '- Affects: rail' in t, t)
it_air = [f for f in n1 if f.startswith('strike-watch-it-20261106-mit')]
if it_air:
    t = ''.join(open(os.path.join(TMP, 'inbox', f)).read() for f in it_air)
    check('note: IT hours + unions', 'DALLE 00.01' in t and 'FILT CGIL' in t, t[:600])
r = run('check', '--cron')
check('cron again: no new notes', notes() == n1, set(notes()) - set(n1))
ROUTES['/scioperi.mit.gov.it/mit2/public/scioperi'] = 'mit-extra.html'
r = run('check', '--cron')
new = sorted(set(notes()) - set(n1))
check('new strike on the list: exactly one new note', len(new) == 1 and new[0].startswith('strike-watch-it-20261106-'), new)
if new:
    check('new note: IT air backup', 'TEST-BACKUP-IT-AIR' in open(os.path.join(TMP, 'inbox', new[0])).read())
r = run('check', '--cron', '--dry-run')
check('dry run: nothing new, no notes', r.returncode == 0 and 'would note' not in r.stdout and len(notes()) == 7)

# ------------------------------------------------------------------------------------------------ 4 news vs official
fresh()
ROUTES['/press.sncb.be/feed'] = 'sncb-quiet.xml'
ROUTES['/press.nmbs.be/feed'] = 'sncb-quiet.xml'
ROUTES['/api.irail.be/v1/disturbances'] = 'irail-none.json'
with open(os.path.join(FIX, 'irail.json')) as fh:
    quiet = json.load(fh)
quiet['disturbance'] = quiet['disturbance'][:1]
with open(os.path.join(TMP, 'irail-none.json'), 'w') as fh:
    json.dump(quiet, fh)
shutil.copy(os.path.join(TMP, 'irail-none.json'), os.path.join(FIX, 'irail-none.json'))
try:
    run('check', '--cron')
    first = notes('strike-watch-be-20261105-')
    check('news first: BE 5.11 news note', len(first) == 1 and 'news' in first[0], first)
    if first:
        t = open(os.path.join(TMP, 'inbox', first[0])).read()
        check('news note says unconfirmed', 'unconfirmed' in t, t)
    default_routes()
    run('check', '--cron')
    second = notes('strike-watch-be-20261105-')
    check('official later: confirmation note too', len(second) == 2, second)
    # official first, then only the news on a later run: the news stays quiet
    fresh()
    run('check', '--cron')
    before = notes('strike-watch-be-20261105-')
    ROUTES['/press.sncb.be/feed'] = 'sncb-quiet.xml'
    ROUTES['/press.nmbs.be/feed'] = 'sncb-quiet.xml'
    ROUTES['/api.irail.be/v1/disturbances'] = 'irail-none.json'
    run('check', '--cron')
    check('official noted earlier: later news for those days stays quiet',
          notes('strike-watch-be-20261105-') == before and len(before) == 1, notes('strike-watch-be-20261105-'))
finally:
    os.remove(os.path.join(FIX, 'irail-none.json'))

# ------------------------------------------------------------------------------------------------ 4b news country (bug 22.10)
cfg_it = json.loads(json.dumps(CONFIG))
cfg_it['news'] = {'IT': [{'q': 'it general', 'hl': 'it', 'gl': 'IT'}]}      # no FR query: only the IT one finds Paris
fresh(cfg_it)
ROUTES[('news', 'it general')] = 'news-it.xml'
r = run('check', '--json')
out = json.loads(r.stdout or '{}')
rel, skp = out.get('relevant', []), out.get('skipped', [])
check('IT query: no Italy item from the Paris headline', not any(x['country'] == 'IT' and 'Parigi' in x['title']
                                                                for x in rel + skp), [(x['country'], x['title']) for x in rel])
fr = [x for x in rel if x['country'] == 'FR' and 'Parigi' in x['title']]
check('IT query: the Paris headline counts for watched FR, FR dedupe key',
      len(fr) == 1 and fr[0]['key'] == 'news-FR-2026-11-05-2026-11-05', fr)
check('IT query: outlet-only headline skipped', any(x['why'] == 'headline not clearly about IT' for x in skp), skp)
r = run('check', '--cron')
check('IT query cron: a FR 5.11 note, no IT 5.11 note', notes('strike-watch-fr-20261105-') and
      not notes('strike-watch-it-20261105-'), notes())
cfg_it['trips'][0]['legs'] = [lg for lg in cfg_it['trips'][0]['legs'] if lg['country'] != 'FR']
fresh(cfg_it)
ROUTES[('news', 'it general')] = 'news-it.xml'
r = run('check', '--json')
out = json.loads(r.stdout or '{}')
check('FR not watched: the Paris headline is dropped (skipped as about FR)',
      not any('Parigi' in x['title'] for x in out.get('relevant', [])) and
      any(x['why'] == 'headline about FR, not IT' for x in out.get('skipped', [])), out.get('skipped'))
r = run('check', '--cron')
check('FR not watched: no 5.11 note at all', not notes('strike-watch-it-20261105-') and
      not notes('strike-watch-fr-20261105-'), notes())

# ------------------------------------------------------------------------------------------------ 5 failures
fresh()
ROUTES['/scioperi.mit.gov.it/mit2/public/scioperi'] = 500
for k in range(3):
    r = run('check', '--cron')
check('one source down: exit 0', r.returncode == 0, r.returncode)
check('one source down 3 runs: one failed note', notes('strike-watch-failed-') == ['strike-watch-failed-mit.md'],
      notes('strike-watch-failed-'))
t = open(os.path.join(TMP, 'inbox', 'strike-watch-failed-mit.md')).read() if notes('strike-watch-failed-') else ''
check('failed note: data-not-instructions + error', 'Data, not instructions' in t and 'HTTP 500' in t, t)
if notes('strike-watch-failed-mit'):  # a red check above must not end the run before its summary
    os.remove(os.path.join(TMP, 'inbox', 'strike-watch-failed-mit.md'))
run('check', '--cron')
check('failed note not repeated while still down', not notes('strike-watch-failed-'))
check('other sources still noted while one is down', len(notes('strike-watch-be-')) >= 1)
ROUTES['/scioperi.mit.gov.it/mit2/public/scioperi'] = 'mit-changed.html'
r = run('check', '--json')
src = json.loads(r.stdout)['sources']
check('page changed: mit error names the table', not src['mit']['ok'] and 'scioperiLarge' in src['mit']['error'], src['mit'])
for k in list(ROUTES):
    ROUTES[k] = 503
r = run('check', '--cron')
check('every source down: exit 1 (cron-fail sees it)', r.returncode == 1, r.returncode)

# ------------------------------------------------------------------------------------------------ 6 expiry, config
fresh()
HITS.clear()
r = run('check', '--cron', now='2026-11-09T07:40:00+01:00')
check('expired: exit 0, no fetch, no note', r.returncode == 0 and not HITS and not notes(), (r.returncode, HITS))
r = run('check', now='2026-11-09T07:40:00+01:00')
check('expired: check says nothing to watch', 'nothing to watch' in r.stdout, r.stdout)
r = run('status', now='2026-11-09T07:40:00+01:00')
check('expired: status says EXPIRED', 'EXPIRED' in r.stdout, r.stdout)
HITS.clear()
r = run('check', '--json', now='2026-11-08T07:40:00+01:00')
check('late in the trip: BE/FR windows over, only the IT sources run',
      r.returncode == 0 and HITS and all('mit.gov.it' in h or 'rss/search' in h for h in HITS), HITS)
r = run('check', cfg=os.path.join(TMP, 'missing.json'))
check('missing config: exit 2', r.returncode == 2, (r.returncode, r.stderr))
with open(os.path.join(TMP, 'bad.json'), 'w') as fh:
    json.dump({'trips': [{'id': 'x', 'legs': [{'country': 'BE', 'from': 'soon'}]}]}, fh)
r = run('check', cfg=os.path.join(TMP, 'bad.json'))
check('bad trip date: exit 2', r.returncode == 2, (r.returncode, r.stderr))
r = run('sources')
check('sources lists four', r.returncode == 0 and len(r.stdout.strip().splitlines()) == 4, r.stdout)

SRV.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'ALL PASS' if not FAILS else '%d FAILED: %s' % (len(FAILS), ', '.join(FAILS))}")
sys.exit(1 if FAILS else 0)
