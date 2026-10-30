"""Offline tests for ~/bin/rave-radar (RAVE_RADAR_BIN=/path/to/copy tests a copy). Synthetic fixtures only: fake
venues, artists and coordinates; RAVE_RADAR_FIXTURES serves every URL from ./fixtures, so nothing touches the network."""
import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, 'fixtures')
BIN = os.environ.get('RAVE_RADAR_BIN') or os.path.expanduser('~/bin/rave-radar')
if not os.path.isfile(BIN):                       # test-all runs under a temp HOME: fall back to the live tree
    BIN = '/home/node/bin/rave-radar'
NOW = '2026-10-18'


def load_module():
    loader = importlib.machinery.SourceFileLoader('rave_radar', BIN)
    spec = importlib.util.spec_from_loader('rave_radar', loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


rr = load_module()

SOURCES = [
    {'id': 'alpha', 'kind': 'html', 'url': 'https://club-alpha.example/programm', 'name': 'Club Alpha',
     'city': 'Alphastadt', 'lat': 47.05, 'lon': 8.05},
    {'id': 'beta', 'kind': 'html', 'url': 'https://hall-beta.example/', 'name': 'Hall Beta', 'city': 'Betaville',
     'lat': 47.3, 'lon': 8.3, 'date_side': 'after'},
    {'id': 'shop', 'kind': 'html', 'url': 'https://shop.example/en', 'name': 'Shop', 'multi': True},
    {'id': 'gamma', 'kind': 'jsonld', 'url': 'https://gamma.example/', 'name': 'Gamma'},
    {'id': 'delta', 'kind': 'wp', 'url': 'https://club-delta.example/wp-json/wp/v2/events', 'name': 'Club Delta',
     'city': 'Alphastadt', 'lat': 47.01, 'lon': 8.01},
    {'id': 'bigfest', 'kind': 'lineup', 'url': 'https://bigfest.example/', 'name': 'Big Fest 2027',
     'city': 'Farland', 'lat': 52.0, 'lon': 5.0, 'travel': True, 'date': '2027-07-10'},
    {'id': 'nearfest', 'kind': 'lineup', 'url': 'https://nearfest.example/', 'name': 'Near Fest', 'lat': 52.0,
     'lon': 5.0},
    {'id': 'oldfest', 'kind': 'lineup', 'url': 'https://bigfest.example/', 'name': 'Old Fest', 'travel': True,
     'require': '2028'},
    {'id': 'empty', 'kind': 'lineup', 'url': 'https://empty-fest.example/', 'name': 'JS Fest', 'travel': True},
    {'id': 'robots', 'kind': 'html', 'url': 'https://epsilon.example/private/list', 'name': 'Epsilon', 'lat': 47.0,
     'lon': 8.0},
    {'id': 'robots-ua', 'kind': 'html', 'url': 'https://epsilon.example/secret/list', 'name': 'Epsilon', 'lat': 47.0,
     'lon': 8.0},
    {'id': 'refused', 'kind': 'html', 'url': 'https://www.songkick.com/artists/1-x/calendar', 'name': 'SK'},
    {'id': 'broken', 'kind': 'html', 'url': 'https://broken.example/', 'name': 'Broken', 'lat': 47.0, 'lon': 8.0},
    {'id': 'off', 'kind': 'html', 'url': 'https://off.example/', 'name': 'Off', 'disabled': True},
]
URLS = {
    'https://club-alpha.example/programm': 'venue-before.html',
    'https://hall-beta.example/': 'venue-after.html',
    'https://shop.example/en': 'shop-multi.html',
    'https://gamma.example/': 'jsonld.html',
    'https://club-delta.example/wp-json/wp/v2/events': 'wp.json',
    'https://bigfest.example/': 'lineup.html',
    'https://nearfest.example/': 'lineup.html',
    'https://empty-fest.example/': 'lineup-empty.html',
    'https://epsilon.example/robots.txt': 'robots-epsilon.txt',
    'https://epsilon.example/private/list': 'epsilon.html',
    'https://epsilon.example/secret/list': 'epsilon.html',
    'https://broken.example/': {'status': 500},
}
PLACES = {'Alphastadt': [47.02, 8.02], 'Fernstadt': [49.5, 12.0], 'Hall Beta': [47.3, 8.3, 'Betaville']}
ARTISTS = [{'name': 'DJ Nova', 'aliases': [], 'source': 'test'},
           {'name': 'Kai Ferro', 'aliases': [], 'source': 'test'},
           {'name': 'Mëga Star', 'aliases': ['Megastar'], 'source': 'test'},
           {'name': 'Jon Peak', 'aliases': [], 'source': 'test'}]


class Env:
    def __init__(self, sources=None, urls=None):
        self.tmp = tempfile.mkdtemp(prefix='rave-radar-test-')
        self.cfg = os.path.join(self.tmp, 'config')
        self.fix = os.path.join(self.tmp, 'fixtures')
        shutil.copytree(FIX, self.fix)
        os.makedirs(self.cfg)
        os.makedirs(os.path.join(self.tmp, 'inbox'))
        self.write_config(sources or SOURCES)
        self.write_urls(urls or URLS)
        with open(os.path.join(self.cfg, 'artists.json'), 'w') as f:
            json.dump({'version': 1, 'artists': ARTISTS}, f)
        self.env = {**{k: v for k, v in os.environ.items() if not k.startswith('RAVE_RADAR_')},
                    'RAVE_RADAR_CONFIG_DIR': self.cfg, 'RAVE_RADAR_STATE_DIR': os.path.join(self.tmp, 'state'),
                    'RAVE_RADAR_CACHE_DIR': os.path.join(self.tmp, 'cache'),
                    'RAVE_RADAR_INBOX': os.path.join(self.tmp, 'inbox'), 'RAVE_RADAR_FIXTURES': self.fix,
                    'RAVE_RADAR_NOW': NOW, 'RAVE_RADAR_HOST_GAP': '0',
                    'ASSISTANT_PROFILE': os.path.join(FIX, 'profile.json')}

    def write_config(self, sources):
        with open(os.path.join(self.cfg, 'config.json'), 'w') as f:
            json.dump({'max_km': 150, 'places': PLACES, 'sources': sources}, f)

    def write_urls(self, urls):
        with open(os.path.join(self.fix, 'urls.json'), 'w') as f:
            json.dump(urls, f)

    def run(self, *args, now=None):
        env = dict(self.env)
        if now:
            env['RAVE_RADAR_NOW'] = now
        p = subprocess.run([sys.executable, BIN, *args], env=env, capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout, p.stderr

    def scan_json(self, *extra, now=None):
        code, out, err = self.run('scan', '--json', *extra, now=now)
        if code not in (0, 1):
            raise AssertionError(f'scan failed {code}: {err}')
        return code, json.loads(out)

    def notes(self):
        d = os.path.join(self.tmp, 'inbox')
        return sorted((os.path.join(d, f) for f in os.listdir(d) if f.startswith('rave-radar-')),
                      key=lambda p: os.stat(p).st_mtime_ns)

    def state(self):
        with open(os.path.join(self.tmp, 'state', 'state.json')) as f:
            return json.load(f)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class Units(unittest.TestCase):
    ref = dt.date(2026, 10, 18)

    def dates(self, text, ref=None):
        return [d.isoformat() for _, d in rr.find_dates(text, ref or self.ref)]

    def test_dates(self):
        self.assertEqual(self.dates('Sa 24.10.2026'), ['2026-10-24'])
        self.assertEqual(self.dates('30.–31.10.2026'), ['2026-10-30'])
        self.assertEqual(self.dates('24. Oktober 2026 und 3 Nov 2026'), ['2026-10-24', '2026-11-03'])
        self.assertEqual(self.dates('Oct 24, 2026'), ['2026-10-24'])
        self.assertEqual(self.dates('2026-11-02T20:00'), ['2026-11-02'])
        self.assertEqual(self.dates('Fr 24.10.26'), ['2026-10-24'])
        self.assertEqual(self.dates('Doors 20.00 Uhr, CHF 25.50, 1.5 km'), [])
        self.assertEqual(self.dates('05.01.', dt.date(2026, 12, 20)), ['2027-01-05'])
        self.assertEqual(self.dates('10.10.', dt.date(2026, 10, 18)), ['2026-10-10'])   # 8 days back: this year
        self.assertEqual(self.dates('31.02.2026 and 32.10.2026'), [])

    def test_norm_and_matcher(self):
        self.assertEqual(rr.norm('Tiësto'), rr.norm('TIESTO'))
        self.assertEqual(rr.norm('A&B'), 'a and b')
        m = rr.Matcher(ARTISTS)
        self.assertEqual(m.find('MEGA STAR live'), ['Mëga Star'])
        self.assertEqual(m.find('a Megastar tribute'), ['Mëga Star'])
        self.assertEqual(m.find('Peak Party, not Jon'), [])
        self.assertEqual(m.find('DJ Novak'), [])
        self.assertEqual(m.find('dj nova b2b kai ferro'), ['DJ Nova', 'Kai Ferro'])

    def test_robots(self):
        txt = 'User-agent: *\nDisallow: /private\nAllow: /private/ok$\nDisallow: /*?sort=\n'
        self.assertFalse(rr.robots_allowed(txt, 'rave-radar', 'https://x.example/private/a'))
        self.assertTrue(rr.robots_allowed(txt, 'rave-radar', 'https://x.example/private/ok'))
        self.assertFalse(rr.robots_allowed(txt, 'rave-radar', 'https://x.example/list?sort=date'))
        self.assertTrue(rr.robots_allowed(txt, 'rave-radar', 'https://x.example/'))
        own = txt + '\nUser-agent: rave-radar\nDisallow: /secret\n'
        self.assertTrue(rr.robots_allowed(own, 'rave-radar', 'https://x.example/private/a'))  # own group only
        self.assertFalse(rr.robots_allowed(own, 'rave-radar', 'https://x.example/secret/a'))

    def test_refused_hosts(self):
        self.assertTrue(rr.refused('https://ra.co/events/1'))
        self.assertTrue(rr.refused('https://www.songkick.com/artists/1'))
        self.assertTrue(rr.refused('https://rest.bandsintown.com/artists/x/events'))
        self.assertIsNone(rr.refused('https://club-alpha.example/'))

    def test_presale(self):
        self.assertEqual(rr.presale_in('Vorverkauf ab 20.10.2026, 10:00', self.ref)[0], '2026-10-20')
        self.assertEqual(rr.presale_in('Presale starts Oct 21, 2026', self.ref)[0], '2026-10-21')
        self.assertEqual(rr.presale_in('Tickets ab CHF 48', self.ref)[0], None)
        self.assertEqual(rr.presale_in('VVK ab 01.10.2026', self.ref)[0], None)    # already past

    def test_cards(self):
        lines = rr.page_lines(open(os.path.join(FIX, 'venue-before.html')).read(), 'https://club-alpha.example/p')
        cs = rr.cards(lines, self.ref, 'before')
        self.assertEqual([d.isoformat() for d, _ in cs],
                         ['2026-10-24', '2026-10-30', '2027-01-05', '2026-10-01', '2026-11-12', '2026-11-20'])
        self.assertIn('https://club-alpha.example/e/nova-24', [u for _, ls in cs[0][1] for u in ls])
        self.assertFalse(any('instagram' in u for _, ls in lines for u in ls))
        self.assertFalse(any('01.01.2030' in t for t, _ in lines))                  # scripts dropped

    def test_places(self):
        self.assertEqual(rr.place_in('Hall Beta, Mainroad 1, 1000 Betaville', PLACES), ('Hall Beta', 'Betaville', 47.3, 8.3))
        self.assertEqual(rr.place_in('Hall Beta, Mainroad 1, Alphastadt', PLACES), ('Hall Beta', 'Alphastadt', 47.02, 8.02))
        self.assertEqual(rr.place_in('Arena, Fernstadt', PLACES)[1], 'Fernstadt')
        self.assertEqual(rr.place_in('nowhere', PLACES), (None, None, None, None))
        # a city named in the text wins over the venue's own city; the venue's own city keeps the venue coordinates
        self.assertEqual(rr.place_in('Hall Beta, Fernstadt', PLACES), ('Hall Beta', 'Fernstadt', 49.5, 12.0))
        self.assertEqual(rr.place_in('Hall Beta, Betaville', {**PLACES, 'Betaville': [47.31, 8.31]}),
                         ('Hall Beta', 'Betaville', 47.3, 8.3))


class Scan(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.close()

    def by(self, res, kind='new'):
        return {(ev['source'], ev['artist'], ev['date']): ev for ev in res[kind]}

    def test_first_scan(self):
        code, res = self.e.scan_json()
        self.assertEqual(code, 0)
        new = self.by(res)
        # html, date before the title: in range, presale from the card, sold out, past and same-day presale dropped
        nova = new[('alpha', 'DJ Nova', '2026-10-24')]
        self.assertEqual(nova['presale']['date'], '2026-10-20')
        self.assertEqual(nova['url'], 'https://club-alpha.example/e/nova-24')
        self.assertTrue(new[('alpha', 'Kai Ferro', '2026-10-30')]['soldout'])
        self.assertNotIn(('alpha', 'DJ Nova', '2026-10-01'), new)
        self.assertIsNone(new[('alpha', 'DJ Nova', '2026-11-12')]['presale'])
        self.assertFalse(any(k[1] == 'Jon Peak' for k in new))
        # html, date after the title; accents and aliases
        self.assertIn(('beta', 'Mëga Star', '2026-11-06'), new)
        self.assertIn(('beta', 'Mëga Star', '2026-11-14'), new)
        self.assertEqual(new[('beta', 'Mëga Star', '2026-11-06')]['url'], 'https://hall-beta.example/show/1')
        # multi-venue shop: place from the card; far city and unknown place are out of range
        shop = new[('shop', 'DJ Nova', '2026-11-07')]
        self.assertEqual((shop['venue'], shop['city']), ('Hall Beta', 'Betaville'))
        self.assertNotIn(('shop', 'DJ Nova', '2026-11-08'), new)
        self.assertNotIn(('shop', 'DJ Nova', '2026-11-09'), new)
        # JSON-LD: performer match, geo, offers.validFrom as on-sale date
        gamma = new[('gamma', 'Kai Ferro', '2026-12-05')]
        self.assertEqual(gamma['presale']['date'], '2026-11-01')
        self.assertEqual(gamma['url'], 'https://tickets-gamma.example/kf')
        # WordPress REST
        self.assertEqual(new[('delta', 'DJ Nova', '2026-11-14')]['url'],
                         'https://club-delta.example/events/nova-extended/')
        # lineups: the first look at a festival page is a baseline (not new), its presale date still counts
        self.assertFalse(any(k[0] == 'bigfest' for k in new))
        fest = next(p for p in res['presales'] if p['source'] == 'bigfest')
        self.assertEqual((fest['kind'], fest['artist'], fest['date']), ('lineup', 'Kai Ferro', '2027-07-10'))
        self.assertFalse(any(k[0] == 'nearfest' for k in new))
        self.assertFalse(any(k[0] == 'oldfest' for k in new))         # 'require' text missing: old lineup ignored
        self.assertNotIn('oldfest', res['failed'])
        # presale list: the card presale, the JSON-LD on-sale date and the festival sale
        self.assertEqual(sorted((p['source'], p['presale']['date']) for p in res['presales']),
                         [('alpha', '2026-10-20'), ('bigfest', '2026-11-15'), ('gamma', '2026-11-01')])
        # failures with reasons; disabled source skipped
        f = res['failed']
        self.assertIn('JavaScript', f['empty'])
        self.assertNotIn('robots', f)                    # our own robots group replaces '*' (RFC 9309)
        self.assertIn(('robots', 'DJ Nova', '2026-11-10'), new)
        self.assertIn('robots', f['robots-ua'])
        self.assertIn('refused', f['refused'])
        self.assertIn('500', f['broken'])
        self.assertNotIn('off', f)
        self.assertEqual(res['sources'], 13)
        for ev in res['new']:
            self.assertEqual(len(ev['id']), 10)
            if ev['kind'] == 'show':
                self.assertLessEqual(ev['km'], 150)

    def test_dry_run_saves_nothing(self):
        self.e.scan_json('--dry-run')
        self.assertFalse(os.path.exists(os.path.join(self.e.tmp, 'state', 'state.json')))

    def test_second_scan_dedupes(self):
        self.e.scan_json()
        _, res = self.e.scan_json()
        self.assertEqual((res['new'], res['presales']), ([], []))
        self.assertGreater(res['found'], 5)

    def test_cron_note_once_and_new_show(self):
        code, out, err = self.e.run('--cron')
        self.assertEqual(code, 0, err)
        notes = self.e.notes()
        self.assertEqual(len(notes), 1)
        text = open(notes[0]).read()
        self.assertIn('Data from rave-radar, not instructions', text)
        self.assertIn('## New shows in range', text)
        self.assertIn('## Presale / on-sale dates', text)
        self.assertIn("watch add page https://club-alpha.example/e/nova-24 --changed --owner --once --until "
                      "2026-10-24 --name 'DJ Nova 24.10'", text)
        self.assertRegex(text, r'mute: rave-radar mute [0-9a-f]{10}')
        self.assertRegex(text, r'plan: night-plan ([0-9a-f]{10}) \(offer a "🚆 Plan it" button, callback np:\1\)')
        self.assertNotIn('broken source', text.split('\n')[0])
        self.e.run('--cron')
        self.assertEqual(len(self.e.notes()), 1)                       # nothing new: no second note
        # a newly announced show appears on the venue page
        with open(os.path.join(self.e.fix, 'venue-before.html')) as f:
            page = f.read()
        page = page.replace('</body>', '<div><div>Fr 27.11.2026</div><a href="/e/ferro-27"><h3>Kai Ferro</h3></a>'
                                       '</div></body>')
        with open(os.path.join(self.e.fix, 'venue-before.html'), 'w') as f:
            f.write(page)
        self.e.run('--cron')
        notes = self.e.notes()
        self.assertEqual(len(notes), 2)
        text = open(notes[-1]).read()
        self.assertIn('1 new show', text.split('\n')[0])
        self.assertIn('Fri 27.11.2026  Kai Ferro', text)
        self.assertNotIn('DJ Nova', text)

    def test_lineup_announced_later(self):
        self.e.scan_json()
        self.assertTrue(any(e['kind'] == 'lineup' for e in json.loads(self.e.run('list', '--days', '400', '--json')[1])))
        with open(os.path.join(self.e.fix, 'lineup.html')) as f:
            page = f.read()
        with open(os.path.join(self.e.fix, 'lineup.html'), 'w') as f:
            f.write(page.replace('<li>Some Act</li>', '<li>DJ Nova</li>'))
        self.e.run('--cron')
        text = open(self.e.notes()[-1]).read()
        self.assertIn('## Festival lineups with his artists', text)
        self.assertIn('DJ Nova in the Big Fest 2027 lineup', text)
        self.assertNotIn('Kai Ferro in the Big Fest', text.split('## Presale')[0])
        self.assertNotIn('Near Fest', text)                            # far and not a travel festival

    def test_presale_found_later_reported_once(self):
        self.e.scan_json()
        with open(os.path.join(self.e.fix, 'venue-after.html')) as f:
            page = f.read()
        page = page.replace('<p>The big one</p>', '<p>The big one. Presale: 22.10.2026</p>')
        with open(os.path.join(self.e.fix, 'venue-after.html'), 'w') as f:
            f.write(page)
        _, res = self.e.scan_json()
        self.assertEqual(res['new'], [])
        self.assertEqual([(p['artist'], p['presale']['date']) for p in res['presales']], [('Mëga Star', '2026-10-22')])
        _, res = self.e.scan_json()
        self.assertEqual(res['presales'], [])

    def test_mute_and_list(self):
        _, res = self.e.scan_json()
        eid = next(ev['id'] for ev in res['new'] if ev['date'] == '2026-10-24')
        code, out, _ = self.e.run('mute', eid)
        self.assertEqual(code, 0)
        self.assertIn('DJ Nova', out)
        code, out, _ = self.e.run('list', '--days', '30')
        self.assertNotIn(eid, out)
        self.assertIn('Kai Ferro', out)                                 # 30.10 is within 30 days
        self.assertNotIn('2026-12-05', out)
        self.assertNotIn('05.12.2026', out)                             # beyond 30 days
        code, out, _ = self.e.run('list', '--days', '30', '--all')
        self.assertIn(f'MUTED    {eid}', out)
        code, out, _ = self.e.run('list', '--json')
        self.assertTrue(all(e['id'] != eid for e in json.loads(out)))
        self.assertEqual(self.e.run('mute', 'nonsense')[0], 2)
        self.e.run('unmute', eid)
        self.assertIn(eid, self.e.run('list')[1])

    def test_muted_presale_not_reported(self):
        _, res = self.e.scan_json('--dry-run')
        eid = next(p['id'] for p in res['presales'] if p['source'] == 'gamma')
        self.e.run('mute', eid)
        _, res = self.e.scan_json()
        self.assertNotIn(eid, [p['id'] for p in res['presales']] + [n['id'] for n in res['new']])

    def test_events_age_out(self):
        self.e.scan_json()
        _, res = self.e.scan_json(now='2026-11-10')
        st = self.e.state()
        self.assertFalse(any((v['date'] or '9999') < '2026-11-07' for v in st['events'].values()))

    def test_source_failure_streak(self):
        for _ in range(2):
            self.e.run('--cron')
        first = len(self.e.notes())
        self.e.run('--cron')                                            # third failure in a row: reported once
        notes = self.e.notes()
        self.assertEqual(len(notes), first + 1)
        text = open(notes[-1]).read()
        self.assertIn('Sources failing 3+ scans in a row', text)
        self.assertIn('- broken: HTTP 500 (3x)', text)
        self.e.run('--cron')
        self.assertEqual(len(self.e.notes()), first + 1)
        code, out, _ = self.e.run('sources')
        self.assertIn('FAIL x4', out)
        self.assertIn('off', out)

    def test_all_sources_fail_exit_1(self):
        self.e.write_config([s for s in SOURCES if s['id'] in ('broken', 'refused')])
        code, _ = self.e.scan_json()
        self.assertEqual(code, 1)

    def test_no_fixture_means_no_network(self):
        self.e.write_urls({})
        code, res = self.e.scan_json()
        self.assertEqual(code, 1)
        self.assertTrue(all('no fixture' in v or 'refused' in v for v in res['failed'].values()), res['failed'])


class Artists(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        os.remove(os.path.join(self.e.cfg, 'artists.json'))

    def tearDown(self):
        self.e.close()

    def test_add_list_rm_private(self):
        self.assertEqual(self.e.run('scan')[0], 2)                     # no artists yet: usage error
        self.assertEqual(self.e.run('artists', 'add', 'DJ Nova', '--note', 'calendar')[0], 0)
        self.e.run('artists', 'add', 'dj nova', '--alias', 'Nova', '--note', 'chat')
        self.e.run('artists', 'add', 'Kai', 'Ferro')
        f = os.path.join(self.e.cfg, 'artists.json')
        self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
        d = json.load(open(f))['artists']
        self.assertEqual([a['name'] for a in d], ['DJ Nova', 'Kai Ferro'])
        self.assertEqual((d[0]['aliases'], d[0]['source']), (['Nova'], 'calendar; chat'))
        code, out, _ = self.e.run('artists', 'list')
        self.assertIn('DJ Nova (also: Nova) — calendar; chat', out)
        self.assertIn('2 artists', out)
        self.assertEqual(self.e.run('artists', 'rm', 'KAI FERRO')[0], 0)
        self.assertEqual(self.e.run('artists', 'rm', 'Nobody')[0], 1)
        self.assertEqual(len(json.loads(self.e.run('artists', 'list', '--json')[1])), 1)

    def test_help(self):
        code, out, _ = self.e.run('--help')
        self.assertEqual(code, 0)
        self.assertIn('rave-radar scan', out)


if __name__ == '__main__':
    unittest.main(verbosity=1)
