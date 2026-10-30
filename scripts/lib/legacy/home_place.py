"""Home as a door-to-door place for the transport tools (route, night-plan, train-watch).

The home address comes from the profile (home.street, home.postcode, home.town, home.town_aliases); nothing
personal lives in code. transport.opendata.ch takes free addresses but snaps some of them to another street in
town, so the API gets the address's coordinates ("lat,lon", geocoded by swisstopo once and cached per address);
without them it gets the text "PC Town, Street N", which resolves to the right street.

  address()   "Street N, PC Town": home as shown in plans, events and SBB links
  is_home(s)  s means home: "home", the address (any word order), the town or an alias (its station name),
              or the town's stop on the home street ("Town, Street")
  query(s)    the API from/to for s: home -> coordinates (else the address text), anything else unchanged
  label(s)    "home" for home, else s
Env: HOME_PLACE_CACHE (cache file), HOME_PLACE_GEO (geocoder URL; "off" = no lookup), ASSISTANT_PROFILE (tests).
"""
import hashlib, json, os, re, urllib.parse, urllib.request

import assistant_profile as profile

GEO = os.environ.get('HOME_PLACE_GEO') or 'https://api3.geo.admin.ch/rest/services/api/SearchServer'
CACHE = os.environ.get('HOME_PLACE_CACHE') or os.path.expanduser('~/.cache/assistant/home-place.json')
UA = 'home-place/1.0 (personal trip planner)'
_memo = {}


def _parts():
    if 'p' not in _memo:
        street = str(profile.get('home.street', '') or '').strip()
        town = str(profile.get('home.town', '') or '').strip()
        pc = str(profile.get('home.postcode', '') or '').strip()
        aliases = [str(x).strip() for x in (profile.get('home.town_aliases', []) or []) if str(x).strip()]
        _memo['p'] = (street, pc, town, aliases)
    return _memo['p']


def _tokens(s):
    return frozenset(re.findall(r'\w+', str(s or '').casefold()))


def address():
    street, pc, town, _ = _parts()
    tail = ' '.join(x for x in (pc, town) if x)
    return ', '.join(x for x in (street, tail) if x)


def _forms():
    if 'f' not in _memo:
        street, pc, town, aliases = _parts()
        bare = re.sub(r'\s+\d+\s*\w?$', '', street).strip()
        out = set()
        for t in [town] + aliases:
            if not t:
                continue
            out.add(_tokens(t))                              # the town / its station name
            for s in {street, bare} - {''}:
                out.add(_tokens(f'{s} {t}'))
                if pc:
                    out.add(_tokens(f'{s} {pc} {t}'))
        _memo['f'] = out
    return _memo['f']


def is_home(s):
    s = str(s or '').strip()
    if not s:
        return False
    return s.casefold() in ('home', 'zuhause', 'daheim') or (_tokens(s) in _forms() and bool(_parts()[2]))


def label(s):
    return 'home' if is_home(s) else s


def _key(addr):
    return hashlib.sha256(addr.encode()).hexdigest()[:16]


def _geocode(addr):
    """swisstopo address search -> (lat, lon) of the exact house, or None."""
    street = _parts()[0]
    q = urllib.parse.urlencode({'searchText': addr.replace(',', ''), 'type': 'locations', 'origins': 'address',
                                'limit': 5, 'sr': 4326})
    try:
        with urllib.request.urlopen(urllib.request.Request(f'{GEO}?{q}', headers={'User-Agent': UA}), timeout=15) as r:
            data = json.load(r)
    except (OSError, ValueError):
        return None
    want = _tokens(street)
    for res in data.get('results') or []:
        a = res.get('attrs') or {}
        lab = re.sub(r'<[^>]+>', ' ', str(a.get('label') or ''))
        if want <= _tokens(lab) and isinstance(a.get('lat'), (int, float)) and isinstance(a.get('lon'), (int, float)):
            return float(a['lat']), float(a['lon'])
    return None


def coords():
    """(lat, lon) of the home address: cache, else swisstopo (cached 0600), else None."""
    addr = address()
    if not addr:
        return None
    if 'c' in _memo:
        return _memo['c']
    k = _key(addr)
    try:
        with open(CACHE) as f:
            c = json.load(f)
        if c.get('key') == k:
            _memo['c'] = (float(c['lat']), float(c['lon']))
            return _memo['c']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if GEO == 'off':
        return None
    ll = _geocode(addr)
    if ll:
        _memo['c'] = ll
        try:
            os.makedirs(os.path.dirname(CACHE), exist_ok=True)
            tmp = f'{CACHE}.tmp{os.getpid()}'
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump({'key': k, 'lat': ll[0], 'lon': ll[1], 'source': 'swisstopo'}, f)
            os.replace(tmp, CACHE)
        except OSError:
            pass
    return ll


def query(s):
    """What the transport API gets for s: home -> 'lat,lon' (else 'PC Town, Street N'); anything else as is."""
    if not is_home(s):
        return s
    ll = coords()
    if ll:
        return f'{ll[0]:.6f},{ll[1]:.6f}'
    street, pc, town, _ = _parts()
    return f"{' '.join(x for x in (pc, town) if x)}, {street}" if street else town
