"""Offline tests for `rave-radar preshow` (RAVE_RADAR_BIN=/path/to/copy tests a copy). Synthetic MixesDB pages and
Deezer answers are generated into a temp dir; RAVE_RADAR_FIXTURES serves every URL, so nothing touches the network.
Fake artist names, fake event; HOME, state, cache, inbox and the lists script are all temporary."""
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
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.environ.get('RAVE_RADAR_BIN') or os.path.expanduser('~/bin/rave-radar')
if not os.path.isfile(BIN):                       # test-all runs under a temp HOME: fall back to the live tree
    BIN = '/home/node/bin/rave-radar'
API, MDB = 'https://deezer.test', 'https://mixesdb.test'
EVENT = {'id': 'e0e0e0e0e0', 'kind': 'show', 'artist': 'DJ Nova', 'date': '2026-10-30', 'title': 'DJ Nova live',
         'venue': 'Hall Beta', 'city': 'Betaville', 'lat': 47.3, 'lon': 8.3, 'url': 'https://hall-beta.example/',
         'source': 'beta', 'presale': None, 'soldout': False}


def load_module():
    loader = importlib.machinery.SourceFileLoader('rave_radar_ps', BIN)
    spec = importlib.util.spec_from_loader('rave_radar_ps', loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


rr = load_module()


def set_page(rows, yt=None):
    lis = ''.join(f'<li>{r}</li>\n' for r in rows)
    player = (f'<noscript><iframe src="https://www.youtube-nocookie.com/embed/{yt}?start=0"></iframe></noscript>'
              if yt else '')
    return (f'<html><body><div id="bodyContent"><h2 id="Players">Players</h2>{player}'
            f'<div class="mw-heading"><h2 id="Tracklist">Tracklist</h2></div>\n<ol>{lis}</ol>\n'
            '</div><div id="bodyBottom"></div><h2 id="relatedPages">Related mixes</h2><ul>'
            '<li><a href="/w/2026-10-01_-_Other_Act_@_Elsewhere">x</a></li><li>[00] Not - Counted</li></ul></body></html>')


SET_A = ['[00] DJ Nova - Opening Theme', '[05] DJ Nova &amp; Kai Ferro - Night Drive [Fake Label]',
         '[10] Other Act - Sunrise (DJ Nova Remix)', '[15] <i>DJ Nova -&#160;?</i>', '[20] DJ Nova - Fresh One',
         '[25] DJ Nova - Old Classic (Original Mix)']
SET_B = ['[00] DJ Nova - Night Drive', '[06] DJ Nova - Fresh One [Fake Label]', '[12] ? - ?',
         '[18] DJ Nova - Unheard Thing']
SET_OLD = ['[00] DJ Nova - Ancient Tune', '[05] DJ Nova - Fresh One']
CATEGORY = ('<html><body><h2>Images</h2><a href="/w/2026-10-05_-_DJ_Nova_@_Club_Alpha">ignored: before Mixes</a>'
            '<h2 id="Mixes">Mixes (5 out of 5)</h2><ul>'
            '<li><a href="/w/2026-10-10_-_DJ_Nova_@_Club_Alpha,_Alphastadt" title="t">'
            '2026-10-10 - DJ Nova @ Club Alpha, Alphastadt</a></li>'
            '<li><a href="/w/2026-09-2X_-_DJ_Nova_(Live_PA)_@_Big_Fest_2026" class="x">'
            '2026-09-2X - DJ Nova (Live PA) @ Big Fest 2026</a></li>'
            '<li><a href="/w/2026-01_-_DJ_Nova_-_Some_Radio_Mix">2026-01 - DJ Nova - Some Radio Mix</a></li>'
            '<li><a href="/w/2016-1X_-_DJ_Nova_@_Nowhere">2016-1X - DJ Nova @ Nowhere</a></li>'
            '<li><a href="/w/2026-12-24_-_DJ_Nova_-_Future_Mix">2026-12-24 - DJ Nova - Future Mix</a></li>'
            '<li><a href="/w/2026-10-12_-_Other_Act_@_Club_Alpha">2026-10-12 - Other Act @ Club Alpha</a></li>'
            '</ul></body></html>')


def dz_track(tid, title, artist, album):
    return {'id': tid, 'title': title, 'artist': {'name': artist}, 'album': {'id': album},
            'link': f'https://www.deezer.com/track/{tid}'}


ALBUMS = {'data': [
    {'id': 501, 'title': 'Fresh One', 'record_type': 'single', 'release_date': '2026-09-25', 'genre_id': 106},
    {'id': 502, 'title': 'Old Classic', 'record_type': 'ep', 'release_date': '2015-03-01', 'genre_id': 106},
    {'id': 503, 'title': 'Night Drive', 'record_type': 'single', 'release_date': '2026-04-01', 'genre_id': 106},
    {'id': 504, 'title': 'Radio Show Episode 123', 'record_type': 'single', 'release_date': '2026-10-01'}]}
FOUND = {('DJ Nova', 'Fresh One'): [dz_track(9, 'Fresh One (Extended Mix)', 'DJ Nova', 501)],
         ('DJ Nova', 'Old Classic'): [dz_track(8, 'Old Classic', 'DJ Nova', 502)],
         ('DJ Nova', 'Night Drive'): [dz_track(7, 'Night Drive Forever', 'DJ Nova', 503),   # different title
                                      dz_track(6, 'Night Drive (feat. Kai Ferro)', 'DJ Nova', 503)],
         ('Other Act', 'Sunrise (DJ Nova Remix)'): [dz_track(5, 'Sunrise (DJ Nova Remix)', 'Other Act', 777)]}


class Env:
    def __init__(self, sets=('a', 'b', 'old'), category=True, crate=None, events=None, muted=None):
        self.tmp = tempfile.mkdtemp(prefix='rave-radar-ps-test-')
        self.fix, self.cfg = os.path.join(self.tmp, 'fix'), os.path.join(self.tmp, 'config')
        self.state, self.inbox = os.path.join(self.tmp, 'state'), os.path.join(self.tmp, 'inbox')
        for d in (self.fix, self.cfg, self.state, self.inbox):
            os.makedirs(d)
        with open(os.path.join(self.cfg, 'artists.json'), 'w') as f:
            json.dump({'version': 1, 'artists': [{'name': 'DJ Nova', 'aliases': ['Nova'], 'source': 'test'}]}, f)
        with open(os.path.join(self.state, 'state.json'), 'w') as f:
            json.dump({'events': events if events is not None else {EVENT['id']: EVENT}, 'muted': muted or {}}, f)
        with open(os.path.join(self.state, 'releases.json'), 'w') as f:
            json.dump({'map': {'dj nova': {'artist': 'DJ Nova', 'id': 101, 'status': 'ok'}}, 'seen': {},
                       'releases': {}}, f)
        urls = {}

        def put(url, body, name):
            with open(os.path.join(self.fix, name), 'w') as f:
                f.write(body if isinstance(body, str) else json.dumps(body))
            urls[url] = name
        if category:
            put(f'{MDB}/w/Category:DJ_Nova', CATEGORY, 'cat.html')
        else:
            urls[f'{MDB}/w/Category:DJ_Nova'] = {'status': 404}
        pages = {'a': ('2026-10-10_-_DJ_Nova_@_Club_Alpha,_Alphastadt', SET_A, 'AAAAAAAAAAA'),
                 'b': ('2026-09-2X_-_DJ_Nova_(Live_PA)_@_Big_Fest_2026', SET_B, None),
                 'old': ('2026-01_-_DJ_Nova_-_Some_Radio_Mix', SET_OLD, None)}
        for k, (path, rows, yt) in pages.items():
            if k in sets:
                put(f'{MDB}/w/{path}', set_page(rows, yt), f'set-{k}.html')
        put(f'{API}/artist/101/albums?limit=500', ALBUMS, 'albums.json')
        put(f'{API}/artist/101/top?limit=8', {'data': [dz_track(1, 'Big Hit', 'DJ Nova', 502),
                                                       dz_track(2, 'Other Hit', 'DJ Nova', 503)]}, 'top.json')
        titles = {('DJ Nova', 'Opening Theme'), ('DJ Nova', 'Night Drive'), ('Other Act', 'Sunrise (DJ Nova Remix)'),
                  ('DJ Nova', 'Fresh One'), ('DJ Nova', 'Old Classic'), ('DJ Nova', 'Unheard Thing'),
                  ('DJ Nova', 'Ancient Tune')}
        for n, (who, title) in enumerate(sorted(titles)):
            q = urllib.parse.quote(f"{who} {rr.MIX_SUFFIX_RE.sub('', title)}")
            put(f'{API}/search/track?q={q}&limit=10', {'data': FOUND.get((who, title), [])}, f'search-{n}.json')
        with open(os.path.join(self.fix, 'urls.json'), 'w') as f:
            json.dump(urls, f)
        self.lists_log = os.path.join(self.tmp, 'lists.log')
        self.crate = os.path.join(self.tmp, 'crate.json')
        with open(self.crate, 'w') as f:
            json.dump(crate or {'list': 'crate', 'open': [], 'done': []}, f)
        self.lists = os.path.join(self.tmp, 'fake-lists')
        with open(self.lists, 'w') as f:
            f.write('#!/bin/sh\nif [ "$1" = show ]; then cat "$CRATE_JSON"; exit 0; fi\n'
                    'printf "%s\\n" "$@" > "$LISTS_LOG"\necho "added to crate"\n')
        os.chmod(self.lists, 0o755)
        self.env = {**{k: v for k, v in os.environ.items() if not k.startswith('RAVE_RADAR_')},
                    'HOME': self.tmp, 'RAVE_RADAR_CONFIG_DIR': self.cfg, 'RAVE_RADAR_STATE_DIR': self.state,
                    'RAVE_RADAR_CACHE_DIR': os.path.join(self.tmp, 'cache'), 'RAVE_RADAR_INBOX': self.inbox,
                    'RAVE_RADAR_FIXTURES': self.fix, 'RAVE_RADAR_NOW': '2026-10-20', 'RAVE_RADAR_HOST_GAP': '0',
                    'RAVE_RADAR_API_GAP': '0', 'RAVE_RADAR_DEEZER_API': API, 'RAVE_RADAR_MIXESDB': MDB,
                    'RAVE_RADAR_LISTS': self.lists, 'LISTS_LOG': self.lists_log, 'CRATE_JSON': self.crate,
                    'TRACKID_STATE_DIR': os.path.join(self.tmp, 'trackid')}

    def run(self, *args, now=None):
        env = dict(self.env)
        if now:
            env['RAVE_RADAR_NOW'] = now
        return subprocess.run([sys.executable, '-I', BIN, *args], env=env, capture_output=True, text=True,
                              timeout=60)

    def notes(self):
        return sorted(f for f in os.listdir(self.inbox) if f.startswith('preshow-'))

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class Unit(unittest.TestCase):
    def test_wed_before(self):
        self.assertEqual(str(rr.wed_before('2026-10-30')), '2026-10-28')     # Fri -> Wed of the same week
        self.assertEqual(str(rr.wed_before('2026-10-28')), '2026-10-21')     # Wed -> the Wednesday before
        self.assertEqual(str(rr.wed_before('2026-11-02')), '2026-10-28')     # Mon -> last week's Wed
        self.assertEqual(str(rr.wed_before('2026-10-29')), '2026-10-28')     # Thu -> the day before

    def test_tracklist_parse(self):
        rows = rr.parse_tracklist(set_page(SET_A + ['[30] ID - ID', '[35] DJ Nova - Unknown', '...']))
        self.assertEqual(len(rows), 9, rows)                      # related-mix list after the heading is not read
        self.assertEqual(rows[1], {'artist': 'DJ Nova & Kai Ferro', 'title': 'Night Drive', 'label': 'Fake Label',
                                   'minute': '05', 'id': False})
        self.assertTrue(rows[3]['id'] and rows[3]['artist'] == 'DJ Nova')    # "DJ Nova - ?" in italics
        self.assertTrue(rows[6]['id'] and rows[7]['id'] and rows[8]['id'])
        self.assertEqual(rr.parse_tracklist('<html>no list</html>'), [])

    def test_category_sets(self):
        sets = rr.mixesdb_sets(CATEGORY, 'DJ Nova')
        self.assertEqual([s['date'] for s in sets], ['2026-12-24', '2026-10-10', '2026-09-20', '2026-01-01'])
        self.assertEqual(sets[1]['short'], 'Club Alpha, Alphastadt')
        self.assertEqual(sets[2]['short'], 'Big Fest 2026')
        self.assertEqual(sets[3]['short'], 'Some Radio Mix')
        self.assertTrue(sets[1]['url'].endswith('/w/2026-10-10_-_DJ_Nova_@_Club_Alpha,_Alphastadt'))

    def test_title_keys(self):
        self.assertEqual(rr.ps_title_key('Old Classic (Original Mix)'), rr.ps_title_key('Old Classic'))
        self.assertEqual(rr.ps_title_key('Night Drive (feat. Kai Ferro)'), 'night drive')
        self.assertNotEqual(rr.ps_title_key('Sunrise (DJ Nova Remix)'), rr.ps_title_key('Sunrise'))
        self.assertEqual(rr.ps_primary('DJ Nova & Kai Ferro'), 'DJ Nova')
        self.assertEqual(rr.ps_short('Sunrise (Some Very Long Name Remix)'), 'Sunrise (Some Very Long N…')

    def test_crawl_delay_and_refused(self):
        txt = 'User-agent: *\nAllow: /\nUser-agent: badbot\nDisallow: /\nUser-agent: *\nCrawl-delay: 8\n'
        self.assertEqual(rr.robots_delay(txt, 'rave-radar'), 8.0)
        self.assertEqual(rr.robots_delay('User-agent: *\nCrawl-delay: 60\n', 'rave-radar'), 10.0)
        self.assertEqual(rr.robots_delay('User-agent: rave-radar\nCrawl-delay: 2\nUser-agent: *\nCrawl-delay: 9\n',
                                         'rave-radar'), 2.0)
        self.assertTrue(rr.robots_allowed(txt, 'rave-radar', 'https://x.example/a'))
        for u in ('https://www.1001tracklists.com/dj/x/index.html', 'https://www.youtube.com/watch?v=x',
                  'https://trackid.net/x'):
            self.assertTrue(rr.refused(u), u)


class Cli(unittest.TestCase):
    def setUp(self):
        self.e = None

    def tearDown(self):
        if self.e:
            self.e.close()

    def test_card_two_sets(self):
        self.e = Env(crate={'list': 'crate', 'open': [{'n': 1, 'text': 'DJ Nova – Old Classic'}], 'done': []})
        p = self.e.run('preshow', 'nova', '--event', EVENT['id'])          # alias resolves to the artist
        self.assertEqual(p.returncode, 0, p.stderr)
        out = p.stdout
        self.assertIn('# rave-radar preshow: DJ Nova · Fri 30.10.2026 Hall Beta, Betaville', out)
        self.assertIn("🎧 DJ Nova: what's in the sets (Betaville, Fri 30.10.2026)", out)
        self.assertIn('From Club Alpha, Alphastadt 10.10 · Big Fest 2026 20.09', out)
        self.assertNotIn('Some Radio Mix', out)                             # older than 90 days, 2 sets enough
        self.assertIn('Most played (2 sets)', out)
        card = out.split('## Card')[1].split('## Buttons')[0]
        lines = [x for x in card.splitlines() if x[:3] in ('1. ', '2. ', '3. ', '4. ', '5. ', '6. ', '7. ')]
        self.assertEqual(lines[0], '1. DJ Nova & Kai Ferro – Night Drive · 2×')    # 2 sets; Deezer's feat. title
        self.assertEqual(lines[1], '2. Fresh One · 2× · 🆕 out 25.09')
        self.assertIn('3. Opening Theme · ❓', lines)
        self.assertIn('4. Other Act – Sunrise (DJ Nova Remix)', lines)
        self.assertIn('5. Old Classic · ✓ crate', lines)                       # "(Original Mix)" dropped
        self.assertIn('6. Unheard Thing · ❓', lines)
        self.assertIn('- DJ Nova – ID · Club Alpha, Alphastadt [15]', card)
        self.assertIn('- ? – ID · Big Fest 2026 [12]', card)
        self.assertIn('- Fresh One (Single, 25.09.2026)', card)
        self.assertNotIn('Radio Show Episode', card)
        self.assertIn('▶ Club Alpha, Alphastadt https://www.youtube.com/watch?v=AAAAAAAAAAA', card)
        self.assertIn('1001Tracklists not read', card)
        self.assertNotIn('Old Classic', out.split('## Buttons')[1].split('## Links')[0])   # already in the crate
        self.assertIn('youtube.com: ', out)
        st = os.stat(os.path.join(self.e.state, 'preshow.json'))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        ps = json.load(open(os.path.join(self.e.state, 'preshow.json')))
        self.assertIn('DJ Nova – Fresh One', ps['events'][EVENT['id']]['tracks'])
        self.assertEqual(ps['sent'], {})                                    # a manual run is not a sent card
        # a button press adds the track to the crate through rave-radar's crate flow
        rid = next(k for k, v in ps['tracks'].items() if v['crate'] == 'DJ Nova – Unheard Thing')
        p = self.e.run('releases', 'crate', f'rr:crate:{rid}')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(open(self.e.lists_log).read().splitlines(), ['add', 'crate', 'DJ Nova – Unheard Thing'])

    def test_widened_when_one_recent_set(self):
        self.e = Env(sets=('a', 'old'))
        p = self.e.run('preshow', 'DJ Nova', '--days', '30', '--json', '--dry-run')
        self.assertEqual(p.returncode, 0, p.stderr)
        res = json.loads(p.stdout)
        self.assertTrue(res['widened'])
        self.assertEqual([s['date'] for s in res['sets']], ['2026-10-10', '2026-01-01'])
        self.assertIn('3 set pages read, 2 with a tracklist', res['sources']['mixesdb'])   # Big Fest page missing
        self.assertEqual(res['event']['id'], EVENT['id'])                  # no --event: the next known show
        self.assertFalse(os.path.exists(os.path.join(self.e.state, 'preshow.json')))       # dry run saves nothing

    def test_fallback_without_sets(self):
        self.e = Env(category=False)
        p = self.e.run('preshow', 'DJ Nova')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('No public set tracklist from the last 12 months', p.stdout)
        self.assertIn('Best known (Deezer top)\n- Big Hit\n- Other Hit', p.stdout)
        self.assertIn('- Night Drive (Single, 01.04.2026)', p.stdout)
        self.assertIn('mixesdb: no MixesDB category for this name', p.stdout)
        self.assertIn('rr:crate:ps-', p.stdout)

    def test_unknown_event(self):
        self.e = Env()
        p = self.e.run('preshow', '--event', 'ffffffffff')
        self.assertEqual(p.returncode, 1)
        self.assertIn('unknown event', p.stderr)

    def test_cron_only_on_wednesday_before_and_once(self):
        muted = dict(EVENT, id='m0m0m0m0m0', date='2026-10-30')
        self.e = Env(events={EVENT['id']: EVENT, muted['id']: muted}, muted={muted['id']: '2026-10-01'})
        for day in ('2026-10-27', '2026-10-29'):
            self.assertEqual(self.e.run('preshow', '--cron', now=day).returncode, 0)
        self.assertEqual(self.e.notes(), [])
        p = self.e.run('preshow', '--cron', '--dry-run', now='2026-10-28')
        self.assertIn('## Card', p.stdout)
        self.assertEqual(self.e.notes(), [])
        p = self.e.run('preshow', '--cron', now='2026-10-28')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(self.e.notes()), 1)                            # the muted show gets none
        body = open(os.path.join(self.e.inbox, self.e.notes()[0])).read()
        self.assertIn('Nothing was sent.', body)
        self.assertIn('DJ Nova · Fri 30.10.2026', body)
        self.e.run('preshow', '--cron', now='2026-10-28')
        self.assertEqual(len(self.e.notes()), 1)                            # once per show


if __name__ == '__main__':
    unittest.main(verbosity=1)
