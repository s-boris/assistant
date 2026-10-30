"""home_place: address/is_home/label/query, geocoding via a fake SearchServer, the 0600 cache and its fallbacks."""
import http.server, json, os, subprocess, sys, tempfile, threading, unittest

LIB = os.environ.get('HOME_PLACE_LIB') or os.path.expanduser('~/bin/lib')
PROFILE = {'home': {'street': 'Probeweg 7', 'postcode': '1234', 'town': 'Testdorf am See', 'town_aliases': ['Testdorf']}}


class Geo(http.server.BaseHTTPRequestHandler):
    log, mode = [], 'ok'

    def log_message(self, *a):
        pass

    def do_CONNECT(self):
        Geo.log.append('CONNECT')
        self.send_response(403)
        self.end_headers()

    def do_GET(self):
        Geo.log.append(self.path)
        if Geo.mode == 'error':
            body, code = b'{}', 500
        else:   # first hit is another street (must be skipped), then the house
            body, code = json.dumps({'results': [
                {'attrs': {'label': 'Andereweg 7 <b>1234 Testdorf am See</b>', 'lat': 46.0, 'lon': 7.0}},
                {'attrs': {'label': 'Probeweg 7 <b>1234 Testdorf am See</b>', 'lat': 47.123456789, 'lon': 8.5}}]}).encode(), 200
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)


class HomePlace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Geo)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f'http://127.0.0.1:{cls.srv.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = os.path.join(self.tmp.name, 'cache', 'home-place.json')
        self.prof(PROFILE)
        Geo.log.clear()
        Geo.mode = 'ok'

    def tearDown(self):
        self.tmp.cleanup()

    def prof(self, p):
        self.profile = os.path.join(self.tmp.name, 'profile.json')
        with open(self.profile, 'w') as f:
            json.dump(p, f)

    def call(self, expr, geo=None):
        env = dict(os.environ, ASSISTANT_PROFILE=self.profile, HOME_PLACE_CACHE=self.cache,
                   HOME_PLACE_GEO=geo or f'{self.url}/SearchServer', http_proxy=self.url, https_proxy=self.url,
                   no_proxy='127.0.0.1')
        code = f'import sys, json; sys.path.insert(0, {LIB!r}); import home_place as h; print(json.dumps({expr}))'
        r = subprocess.run([sys.executable, '-I', '-c', code], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_address_and_forms(self):
        self.assertEqual(self.call('h.address()'), 'Probeweg 7, 1234 Testdorf am See')
        yes = ['home', 'Home', ' zuhause ', 'Testdorf am See', 'Testdorf', 'testdorf am see', 'Testdorf am See, Probeweg',
               'Testdorf, Probeweg', 'Probeweg 7, Testdorf am See', 'Probeweg 7, 1234 Testdorf am See',
               '1234 Testdorf am See, Probeweg 7', 'Probeweg 7 Testdorf']
        no = ['', 'Testdorf Süd', 'Testdorf am See, Bahnhof', 'Testdorf, Andereweg', 'Bigcity HB', 'Probeweg 8, Testdorf',
              'Am See']
        self.assertEqual(self.call(f'[h.is_home(s) for s in {yes!r}]'), [True] * len(yes))
        self.assertEqual(self.call(f'[h.is_home(s) for s in {no!r}]'), [False] * len(no))
        self.assertEqual(self.call("[h.label('Testdorf'), h.label('Bigcity HB')]"), ['home', 'Bigcity HB'])
        self.assertEqual(Geo.log, [])                                        # forms never geocode

    def test_query_geocodes_once_and_caches(self):
        self.assertEqual(self.call("[h.query('home'), h.query('Bigcity HB')]"), ['47.123457,8.500000', 'Bigcity HB'])
        self.assertEqual(len(Geo.log), 1)
        self.assertIn('Probeweg+7+1234+Testdorf+am+See', Geo.log[0])
        self.assertEqual(os.stat(self.cache).st_mode & 0o777, 0o600)
        with open(self.cache) as f:
            c = json.load(f)
        self.assertEqual((c['lat'], c['lon'], len(c['key'])), (47.123456789, 8.5, 16))
        self.assertNotIn('Probeweg', json.dumps(c))                          # the cache holds no address text
        self.assertEqual(self.call("h.query('Testdorf')"), '47.123457,8.500000')
        self.assertEqual(len(Geo.log), 1)                                    # cache hit: no second lookup

    def test_cache_follows_the_address(self):
        self.call("h.query('home')")
        self.prof({'home': dict(PROFILE['home'], street='Probeweg 9')})
        Geo.mode = 'error'                                                   # moved: old cache is ignored
        self.assertEqual(self.call("h.query('home')"), '1234 Testdorf am See, Probeweg 9')
        self.assertEqual(len(Geo.log), 2)

    def test_fallbacks(self):
        Geo.mode = 'error'
        self.assertEqual(self.call("h.query('home')"), '1234 Testdorf am See, Probeweg 7')
        self.assertFalse(os.path.exists(self.cache))                         # failures are not cached
        Geo.log.clear()
        self.assertEqual(self.call("h.query('home')", geo='off'), '1234 Testdorf am See, Probeweg 7')
        self.assertEqual(Geo.log, [])
        self.prof({'home': {'town': 'Testdorf am See', 'street': '', 'postcode': '', 'town_aliases': []}})   # no street
        self.assertEqual(self.call("[h.is_home('Testdorf am See'), h.query('home'), h.address()]", geo='off'),
                         [True, 'Testdorf am See', 'Testdorf am See'])
        self.prof({'home': {'town': '', 'street': '', 'postcode': '', 'town_aliases': []}})   # no home: nothing is
        self.assertEqual(self.call("[h.is_home('Testdorf'), h.address()]", geo='off'), [False, ''])
        self.assertNotIn('CONNECT', Geo.log)


if __name__ == '__main__':
    unittest.main(verbosity=1)
