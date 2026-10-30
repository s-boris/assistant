"""Fake tool modules for test_deep.py: written into a temp BIN dir, imported by the canary's own load_tool().
Each fake reads its behaviour from a JSON file named by an env var, so one test = one small spec. Synthetic only."""

TRIP = r"""
import datetime as dt, json, os
with open(os.environ['TC_FAKE'], encoding='utf-8') as _f:
    CFG = json.load(_f)
class UsageError(Exception): pass
FORECAST_URL, MARINE_URL, ARCHIVE_URL = 'https://f.example/', 'https://m.example/', 'https://a.example/'
DAILY = MARINE_DAILY = 'x'
FORECAST_DAYS, MARINE_DAYS = 7, 5
CALLS = []
def load_config():
    if CFG.get('no_config'):
        raise UsageError('no trips configured')
    return {'t1': {}}
def check_trip(key, t):
    return {'legs': [{'_from': dt.date(2030, 3, 10)}], 'tz': 'UTC', '_tz': 'UTC',
            'reef': CFG.get('reef'), 'advisory': CFG.get('advisory'), 'bulletin': CFG.get('bulletin')}
def now_in(tz): return dt.datetime(2030, 3, 12, 9)
def om_url(base, legs, a, b, q): return base
def shift_year(d, n): return d.replace(year=d.year + n)
def record(src, name, ok, note): src[name] = {'ok': ok, 'note': note}
def source_json(src, name, url, kind, force):
    s = CFG['om'][name]
    if 'error' in s:
        record(src, name, False, s['error']); return None
    record(src, name, True, s.get('note', ''))
    return s
def per_location(data, n): return None if data.get('shape_bad') else [{'daily': data}]
def by_date(daily):
    v = daily.get('value', 20.5)
    return {f'd{i}': {'temperature_2m_max': v, 'wave_height_max': v} for i in range(daily.get('rows', 0))}
def reef(src, trip, force):
    s = CFG['reef_res']
    record(src, 'reef', 'row' in s, s.get('note', ''))
    return s.get('row')
def advisory(src, trip, force, day):
    s = CFG['advice_res']
    record(src, 'advice', 'updated' in s, s.get('note', ''))
    return {'govuk': {'updated': s['updated']}} if 'updated' in s else {}
def bulletin(src, trip, force, d):
    CALLS.append(d.isoformat())
    s = CFG['bulletin_by_day'].get(d.isoformat()) or {'note': 'bulletin unavailable (HTTP 404)'}
    record(src, trip['bulletin'].get('label') or 'bulletin', 'got' in s, s.get('note', ''))
    return s.get('got')
"""

RAVE = r"""
import json, os
with open(os.environ['RR_FAKE'], encoding='utf-8') as _f:
    CFG = json.load(_f)
REL_F = os.environ['RR_FAKE_REL']
DEEZER_API = 'https://api.deezer.example/2.0'
class FetchError(Exception): pass
def host_of(u): return u.split('/')[2]
def load_config():
    if CFG.get('no_config'):
        raise SystemExit(1)
    return {'sources': CFG['sources'], 'places': {}, 'cache_hours': 20}
def load_artists(): return {'artists': [{'name': 'Nova', 'aliases': []}]}
class Matcher:
    def __init__(self, a): self.a = a
FETCHERS = []
class Fetcher:
    def __init__(self):
        self.api_hosts, self.got = set(), []
        FETCHERS.append(self)
    def get(self, url, max_age):
        self.got.append((url, max_age))
        b = CFG['pages'].get(url)
        if b is None:
            raise FetchError(f'HTTP 500 for {url}')
        return b
def today(): return '2026-10-01'
def _parse(src, body, matcher, ref, places):
    if body == 'gone':
        raise ValueError('event list not found')
    return json.loads(body)
PARSERS = {'html': _parse, 'jsonld': _parse}
def artist_albums(F, aid):
    m = CFG['deezer']
    if m == 'notjson':
        raise FetchError('Deezer: not JSON (maintenance page)')
    if m == 'down':
        raise FetchError('HTTP 503 from Deezer')
    return m
"""

AI = r"""
import json, os
with open(os.environ['AI_FAKE'], encoding='utf-8') as _f:
    CFG = json.load(_f)
SOURCES = CFG['sources']
ONLY = set(CFG.get('only') or [])
FIXTURES = CFG.get('fixtures', '')
HN_POINTS = 100
SEEN = []
def load_state(): return {'robots': {}}
def fetch_body(sp, s, cond=False):
    SEEN.append(sp['_url'])
    b = CFG['bodies'].get(sp['name'])
    if b == '__none__':
        return None
    if b is None:
        raise OSError('HTTP 404')
    return b
def page_lines(body, md): return ([l for l in body.splitlines() if l.startswith('model')], None)
def _items(sp, body): return json.loads(body)
items_feed = items_hn = items_hf = _items
"""

DEALS = r"""
import os
MAX_PAGES = 5
FETCH_MIN_TB = 16
SEEN = {}
def fetch_all(min_tb):
    SEEN.update(state=os.environ.get('DEALS_STATE_DIR'), pages=MAX_PAGES, min_tb=min_tb)
    if os.environ.get('DEALS_FAKE_MARK'):
        open(os.environ['DEALS_FAKE_MARK'], 'w').close()
    return {'ts': 0, 'sources': {'alt': {'label': 'Alternate', 'pages': 1, 'n': 7, 'error': None}}}
"""

DEADLINES = r"""
import json, os
DATA = os.environ['DEADLINES_DATA']
def load_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default
"""

KK = r"""
import os
FILES = {'praemien_ch.csv': ('https://bag.example/praemien_ch.csv', 'Versicherer')}
REGIONS_URL = 'https://priminfo.example/praemienregionen-{year}.xlsx'
UA = 'kk-compare-test/1.0'
DATA = os.environ.get('KK_DATA_DIR', '/nonexistent')
def data_year(p):
    raise FileNotFoundError(p)
"""

ALL = {
    "trip-conditions": TRIP,
    "rave-radar": RAVE,
    "ai-releases": AI,
    "deals": DEALS,
    "deadlines": DEADLINES,
    "kk-compare": KK,
}
