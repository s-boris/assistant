"""Offline tests for `rave-radar releases` (RAVE_RADAR_BIN=/path/to/copy tests a copy). Synthetic Deezer fixtures in
the shape of recorded api.deezer.com answers (fixtures/deezer/); RAVE_RADAR_DEEZER_API points at a fake host that
RAVE_RADAR_FIXTURES serves, so nothing touches the network."""
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
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, 'fixtures')
BIN = os.environ.get('RAVE_RADAR_BIN') or os.path.expanduser('~/bin/rave-radar')
if not os.path.isfile(BIN):                       # test-all runs under a temp HOME: fall back to the live tree
    BIN = '/home/node/bin/rave-radar'
API = 'https://deezer.test'
WEEK1, WEEK2 = '2026-10-18', '2026-10-23'


def load_module():
    loader = importlib.machinery.SourceFileLoader('rave_radar_rel', BIN)
    spec = importlib.util.spec_from_loader('rave_radar_rel', loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


rr = load_module()

ARTISTS = [{'name': 'DJ Nova', 'aliases': [], 'source': 'test'},
           {'name': 'Kai Ferro', 'aliases': [], 'source': 'test'},
           {'name': 'Mëga Star', 'aliases': ['Megastar'], 'source': 'test'},
           {'name': 'Jon Peak', 'aliases': [], 'source': 'test'},
           {'name': 'Sunfest Festival', 'aliases': ['Sunfest'], 'source': 'test'}]
SEARCH = {'DJ%20Nova': 'search-nova.json', 'Kai%20Ferro': 'search-ferro.json', 'M%C3%ABga%20Star': 'search-mega.json',
          'Jon%20Peak': 'search-peak.json'}


def urls(week=1, error=False):
    u = {f'{API}/robots.txt': 'deezer/robots-deny-all.txt'}   # never read: API hosts skip robots.txt
    for q, f in SEARCH.items():
        u[f'{API}/search/artist?q={q}&limit=25'] = 'deezer/error-quota.json' if error else f'deezer/{f}'
    u[f'{API}/artist/101/albums?limit=500'] = 'deezer/albums-101.json'
    u[f'{API}/artist/102/albums?limit=500'] = f"deezer/albums-102{'-week2' if week == 2 else ''}.json"
    u[f'{API}/artist/201/albums?limit=500'] = 'deezer/albums-201-p1.json'
    u[f'{API}/artist/201/albums?limit=500&index=2'] = f"deezer/albums-201{'-week2' if week == 2 else ''}-p2.json"
    u[f'{API}/artist/301/albums?limit=500'] = 'deezer/albums-301.json'
    u[f'{API}/artist/401/albums?limit=500'] = 'deezer/albums-401.json'
    return u


class Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix='rave-radar-rel-test-')
        self.cfg = os.path.join(self.tmp, 'config')
        self.fix = os.path.join(self.tmp, 'fixtures')
        shutil.copytree(FIX, self.fix)
        os.makedirs(self.cfg)
        os.makedirs(os.path.join(self.tmp, 'inbox'))
        self.write_urls(urls())
        with open(os.path.join(self.cfg, 'artists.json'), 'w') as f:
            json.dump({'version': 1, 'artists': ARTISTS}, f)
        self.lists_log = os.path.join(self.tmp, 'lists.log')
        self.lists = os.path.join(self.tmp, 'fake-lists')
        with open(self.lists, 'w') as f:
            f.write('#!/bin/sh\nprintf "%s\\n" "$@" > "$LISTS_LOG"\necho "added to crate"\n')
        os.chmod(self.lists, 0o755)
        self.env = {**{k: v for k, v in os.environ.items() if not k.startswith('RAVE_RADAR_')},
                    'RAVE_RADAR_CONFIG_DIR': self.cfg, 'RAVE_RADAR_STATE_DIR': os.path.join(self.tmp, 'state'),
                    'RAVE_RADAR_CACHE_DIR': os.path.join(self.tmp, 'cache'),
                    'RAVE_RADAR_INBOX': os.path.join(self.tmp, 'inbox'), 'RAVE_RADAR_FIXTURES': self.fix,
                    'RAVE_RADAR_NOW': WEEK1, 'RAVE_RADAR_HOST_GAP': '0', 'RAVE_RADAR_API_GAP': '0',
                    'RAVE_RADAR_DEEZER_API': API, 'RAVE_RADAR_LISTS': self.lists, 'LISTS_LOG': self.lists_log,
                    'ASSISTANT_PROFILE': os.path.join(FIX, 'profile.json')}

    def write_urls(self, u):
        with open(os.path.join(self.fix, 'urls.json'), 'w') as f:
            json.dump(u, f)

    def run(self, *args, now=None):
        env = dict(self.env)
        if now:
            env['RAVE_RADAR_NOW'] = now
        p = subprocess.run([sys.executable, BIN, *args], env=env, capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout, p.stderr

    def rel_json(self, *extra, now=None):
        code, out, err = self.run('releases', '--json', *extra, now=now)
        if code not in (0, 1):
            raise AssertionError(f'releases failed {code}: {err}')
        return code, json.loads(out)

    def notes(self):
        d = os.path.join(self.tmp, 'inbox')
        return sorted(os.path.join(d, f) for f in os.listdir(d))

    def state_file(self):
        return os.path.join(self.tmp, 'state', 'releases.json')

    def state(self):
        with open(self.state_file()) as f:
            return json.load(f)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class Releases(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.close()

    def test_first_run_seeds_silently_and_maps(self):
        code, out, err = self.e.run('releases', '--cron')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.e.notes(), [])                            # the seed writes no note
        self.assertEqual(stat.S_IMODE(os.stat(self.e.state_file()).st_mode), 0o600)
        st = self.e.state()
        m = {v['artist']: v for v in st['map'].values()}
        self.assertEqual((m['DJ Nova']['id'], m['DJ Nova']['status']), (102, 'doubtful'))   # electronic, fewer fans
        self.assertIn('more fans but not electronic: #101', m['DJ Nova']['why'])
        self.assertEqual((m['Kai Ferro']['id'], m['Kai Ferro']['status']), (201, 'ok'))
        self.assertEqual((m['Mëga Star']['id'], m['Mëga Star']['status']), (301, 'ok'))      # matched by alias
        self.assertEqual((m['Jon Peak']['id'], m['Jon Peak']['status']), (None, 'none'))
        self.assertIn('closest: Jon Peaks', m['Jon Peak']['why'])
        self.assertEqual((m['Sunfest Festival']['id'], m['Sunfest Festival']['status']), (None, 'event'))
        # in the window or recent: seen; future (1023) not yet; older than the keep horizon (1020, 3011) not stored
        self.assertEqual(sorted(st['seen']), ['1022', '2013'])          # 2013 comes from the second page
        code, res = self.e.rel_json()
        self.assertEqual((res['new'], res['new_mappings'], res['mapped']), ([], [], 3))

    def test_dry_run_all_lists_window_and_saves_nothing(self):
        code, res = self.e.rel_json('--dry-run', '--all')
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.e.state_file()))
        self.assertEqual(res['new'], [])
        self.assertEqual([(r['id'], r['crate'], r['type']) for r in res['in_window']],
                         [('1022', 'DJ Nova – Old Window Single', 'Single')])
        self.assertEqual(res['window'], ['2026-10-08', WEEK1])
        code, out, _ = self.e.run('releases', '--dry-run', '--all')
        self.assertIn('IN ', out)
        self.assertIn('doubtful  DJ Nova → #102', out)
        self.assertIn('(dry run: nothing saved)', out)

    def test_new_releases_note_once(self):
        self.e.run('releases', '--cron')
        self.e.write_urls(urls(week=2))
        code, out, err = self.e.run('releases', '--cron', now=WEEK2)
        self.assertEqual(code, 0, err)
        notes = self.e.notes()
        self.assertEqual([os.path.basename(n).startswith('rave-radar-releases-') for n in notes], [True])
        text = open(notes[0]).read()
        self.assertIn('# rave-radar releases: 3 new releases', text)
        lines = [ln for ln in text.splitlines() if ln.startswith('- ')]
        self.assertEqual(lines, ['- 1023  DJ Nova – Future Pulse · EP · Fri 23.10.2026',     # future one, now out
                                 '- 2014  Kai Ferro – Steel EP · EP · Wed 21.10.2026',
                                 '- 1021  DJ Nova – Night Drive · Single · Tue 20.10.2026'])
        self.assertIn('rr:crate:1021', text)
        self.assertIn("lists add crate 'DJ Nova – Night Drive'", text)
        self.assertIn('deezer: https://www.deezer.com/album/1021', text)
        self.assertIn('spotify: https://open.spotify.com/search/DJ%20Nova%20Night%20Drive', text)
        self.assertIn('beatport: https://www.beatport.com/search?q=DJ+Nova+Night+Drive', text)
        self.assertIn('on_press "keep"', text)
        self.assertNotIn('Episode 123', text)                           # radio episode skipped
        self.assertNotIn('Lost Archive', text)                          # surfaced late, dated before the window
        self.assertNotIn('Artist mappings to check', text)              # mappings were made in the seed run
        self.e.run('releases', '--cron', now=WEEK2)
        self.assertEqual(len(self.e.notes()), 1)                        # nothing new: no second note

    def test_pin_mapping_seeds_and_crate(self):
        self.e.run('releases', '--cron')
        self.assertEqual(self.e.run('releases', 'map', 'Jon', 'Peak', '401')[0], 0)
        self.assertEqual(self.e.run('releases', 'map', 'Nobody', '1')[0], 1)
        self.assertEqual(self.e.run('releases', 'map', 'Jon Peak', 'x1')[0], 2)
        code, res = self.e.rel_json()
        self.assertEqual((res['new'], res['seeded']), ([], 1))         # a pinned artist seeds silently too
        code, out, _ = self.e.run('releases', 'map')
        self.assertIn('pinned    Jon Peak → #401', out)
        self.assertIn('4/5 mapped', out)
        self.e.write_urls(urls(week=2))
        self.e.run('releases', '--cron', now=WEEK2)
        code, out, _ = self.e.run('releases', 'crate', 'rr:crate:1021', '--dry-run')
        self.assertEqual(out.strip(), f"{self.e.lists} add crate 'DJ Nova – Night Drive'")
        self.assertFalse(os.path.exists(self.e.lists_log))
        code, out, err = self.e.run('releases', 'crate', 'rr:crate:1021')
        self.assertEqual((code, out.strip()), (0, 'added to crate'), err)
        self.assertEqual(open(self.e.lists_log).read().splitlines(), ['add', 'crate', 'DJ Nova – Night Drive'])
        self.assertEqual(self.e.run('releases', 'crate', '999')[0], 1)
        self.assertEqual(self.e.run('releases', 'map', 'Jon Peak', 'none')[0], 0)
        code, res = self.e.rel_json()
        self.assertEqual(res['mapped'], 3)

    def test_api_errors_fail_without_mapping(self):
        self.e.write_urls(urls(error=True))
        code, res = self.e.rel_json()
        self.assertEqual(code, 1)                                       # every artist failed
        self.assertEqual(res['mapped'], 0)
        self.assertIn('Deezer error 4', res['failed']['DJ Nova'])
        st = self.e.state()
        self.assertEqual(list(st['map']), ['sunfest festival'])        # only the event (no request needed)
        self.assertNotIn('seeded_at', st)
        self.e.write_urls(urls())
        code, res = self.e.rel_json()
        self.assertEqual((code, res['mapped']), (0, 3))

    def test_releases_cron_does_not_run_the_show_scan(self):
        self.assertFalse(os.path.exists(os.path.join(self.e.cfg, 'config.json')))   # scan would die: no config
        code, out, err = self.e.run('releases', '--cron')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.e.run('--cron')[0], 2)                    # top-level --cron is still the show scan

    def test_new_artist_mapping_flagged_in_note(self):
        self.e.run('releases', '--cron')
        with open(os.path.join(self.e.cfg, 'artists.json'), 'w') as f:
            json.dump({'artists': ARTISTS + [{'name': 'Jon Peaks'}, {'name': 'Rex Odd'}, {'name': 'Rocky Road'}]}, f)
        u = urls(week=2)
        u[f'{API}/search/artist?q=Rocky%20Road&limit=25'] = 'deezer/search-rock.json'
        u[f'{API}/artist/501/albums?limit=500'] = 'deezer/albums-101.json'          # rock only
        u[f'{API}/search/artist?q=Jon%20Peaks&limit=25'] = 'deezer/search-peak.json'
        u[f'{API}/search/artist?q=Rex%20Odd&limit=25'] = 'deezer/search-peak.json'
        self.e.write_urls(u)
        self.e.run('releases', '--cron', now=WEEK2)
        text = open(self.e.notes()[0]).read()
        self.assertIn('## Artist mappings to check', text)
        self.assertIn('- none      Rex Odd → -  [no exact Deezer match (closest: Jon Peaks, 70000 fans)]', text)
        self.assertIn('- none      Rocky Road → -  [only non-electronic same-name artists: #501 (40000 fans, '
                      'electronic 0.0)]', text)
        self.assertNotIn('Jon Peaks →', text)                           # a clean new mapping is not listed
        self.assertNotIn('Summit', text)                                # its existing releases seed silently


class Units(unittest.TestCase):
    def test_skip_patterns(self):
        import re
        skip = re.compile('|'.join(rr.SKIP_TITLE), re.I)
        for t in ('NOVA 123 - Nova Radio Episode 123', 'UNFILTERED 008', 'Show Radio Top 50 - 2026, Vol. 1',
                  'Weekly Podcast 12', 'ABCD 1234 - Best Of', 'Group Session 698 (DJ Mix)', 'Show 1200 (Mixed)'):
            self.assertTrue(skip.search(t), t)
        for t in ('Night Drive', 'Steel EP', 'In And Out Of Love (Remixes EP)', 'Live at Bigfest 2026', 'ID 2026',
                  'Ibiza 2026', 'Room 101', 'Unfiltered 008', 'Island 2026 (DJ Mix)'):
            self.assertFalse(skip.search(t), t)

    def test_event_names(self):
        for n in ('Sunfest Festival', 'Synergy Events', 'Club Outdoor', 'Open Air Days'):
            self.assertTrue(rr.EVENT_RE.search(n), n)
        for n in ('DJ Nova', 'Festive Kid', 'Eventide'):
            self.assertFalse(rr.EVENT_RE.search(n), n)

    def test_error_body_is_not_cached(self):
        tmp = tempfile.mkdtemp(prefix='rave-radar-rel-unit-')
        try:
            with mock.patch.object(rr, 'CACHE_DIR', tmp), mock.patch.object(rr, 'DEEZER_API', API), \
                    mock.patch.object(rr.time, 'sleep', lambda s: None):
                f = rr.Fetcher()
                f.fixtures = None
                f.api_hosts.add('deezer.test')
                calls = []

                def raw(url, headers):
                    calls.append(url)
                    if len(calls) <= 2:
                        return 200, '{"error": {"code": 4, "message": "Quota limit exceeded"}}', {}
                    return 200, '{"data": [], "total": 0}', {}
                f._raw = raw
                with self.assertRaises(rr.FetchError):
                    rr.deezer(f, '/search/artist?q=x&limit=25', 3600)
                self.assertEqual(len(calls), 2)                         # quota: one retry, robots.txt never read
                self.assertFalse(os.path.isdir(os.path.join(tmp, 'http')))
                self.assertEqual(rr.deezer(f, '/search/artist?q=x&limit=25', 3600), {'data': [], 'total': 0})
                self.assertEqual(len(os.listdir(os.path.join(tmp, 'http'))), 1)
                self.assertEqual(rr.deezer(f, '/search/artist?q=x&limit=25', 3600)['total'], 0)
                self.assertEqual(len(calls), 3)                         # served from the cache
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main(verbosity=1)
