"""flights: flight legs from a ✈️ calendar event (flight numbers, airports, local times), shared by calcache and
flight-watch.

  parse_event(summary, description, start, end) -> [{no, from, to, dep, arr}]
      Legs from lines that start with a flight number ("QR 9101 Frankfurt Sat 14.11 16:10 → Doha Sat 14.11 23:40"):
      text before the first weekday/date/time is the airport (a "(XXX)" code, a known code, or a known city/airport
      name), the first date and time on each side are the local departure/arrival. A missing date carries over from
      the leg before (a connection leaves on or after the previous arrival), else the event start; a missing time on
      the first leg is the event start, on the last leg the event end. Without such lines, a flight number in the
      title with "A → B" makes one leg from the event's start and end. dep/arr are ISO with the airport's UTC
      offset (an unknown airport uses the event's own offset).
  resolve(name) -> IATA code or None; tz_of(code) -> ZoneInfo or None; season(date) -> 'S2026' / 'W2026'
Rail lines (RE 48, IC 5 ...) never count as flights. More airports/airlines: the private ~/.config/flight-watch/
airports.json (env FLIGHTS_EXTRA; see _load_extra).
"""
import datetime as dt
import json
import os
import re
import unicodedata
from zoneinfo import ZoneInfo

# IATA -> (time zone, names). Generic public data; extend as needed.
AIRPORTS = {
    'ZRH': ('Europe/Zurich', 'zurich|zürich|zuerich|kloten|zurich airport|flughafen zürich'),
    'BSL': ('Europe/Zurich', 'basel|basel-mulhouse|euroairport'), 'GVA': ('Europe/Zurich', 'geneva|genf|genève|geneve'),
    'BRN': ('Europe/Zurich', 'bern|belp'), 'LUG': ('Europe/Zurich', 'lugano'),
    'DXB': ('Asia/Dubai', 'dubai'), 'DOH': ('Asia/Qatar', 'doha'),
    'IST': ('Europe/Istanbul', 'istanbul'), 'SAW': ('Europe/Istanbul', 'sabiha gokcen'),
    'MLE': ('Indian/Maldives', 'male|malé|maldives'), 'MRU': ('Indian/Mauritius', 'mauritius'),
    'MXP': ('Europe/Rome', 'malpensa|milan malpensa|mailand malpensa'),
    'LIN': ('Europe/Rome', 'linate|milan|milano|mailand|milan linate'), 'BGY': ('Europe/Rome', 'bergamo|orio al serio'),
    'FCO': ('Europe/Rome', 'rome|roma|rom|fiumicino'), 'VCE': ('Europe/Rome', 'venice|venedig|venezia'),
    'NAP': ('Europe/Rome', 'naples|neapel|napoli'), 'CTA': ('Europe/Rome', 'catania'),
    'BRU': ('Europe/Brussels', 'brussels|brüssel|bruxelles|zaventem'), 'CRL': ('Europe/Brussels', 'charleroi'),
    'AMS': ('Europe/Amsterdam', 'amsterdam|schiphol'), 'CDG': ('Europe/Paris', 'paris|charles de gaulle|paris cdg'),
    'ORY': ('Europe/Paris', 'orly'), 'NCE': ('Europe/Paris', 'nice|nizza'),
    'LHR': ('Europe/London', 'london|heathrow|london heathrow'), 'LGW': ('Europe/London', 'gatwick'),
    'STN': ('Europe/London', 'stansted'), 'LTN': ('Europe/London', 'luton'), 'DUB': ('Europe/Dublin', 'dublin'),
    'FRA': ('Europe/Berlin', 'frankfurt'), 'MUC': ('Europe/Berlin', 'munich|münchen|muenchen'),
    'BER': ('Europe/Berlin', 'berlin'), 'HAM': ('Europe/Berlin', 'hamburg'), 'DUS': ('Europe/Berlin', 'düsseldorf|dusseldorf'),
    'CGN': ('Europe/Berlin', 'cologne|köln|koeln'), 'STR': ('Europe/Berlin', 'stuttgart'),
    'VIE': ('Europe/Vienna', 'vienna|wien'), 'PRG': ('Europe/Prague', 'prague|prag'),
    'BUD': ('Europe/Budapest', 'budapest'), 'WAW': ('Europe/Warsaw', 'warsaw|warschau'),
    'BEG': ('Europe/Belgrade', 'belgrade|beograd|belgrad'), 'ZAG': ('Europe/Zagreb', 'zagreb'),
    'SPU': ('Europe/Zagreb', 'split'), 'DBV': ('Europe/Zagreb', 'dubrovnik'), 'LJU': ('Europe/Ljubljana', 'ljubljana'),
    'ATH': ('Europe/Athens', 'athens|athen'), 'MAD': ('Europe/Madrid', 'madrid'),
    'BCN': ('Europe/Madrid', 'barcelona'), 'PMI': ('Europe/Madrid', 'palma|mallorca|majorca'),
    'IBZ': ('Europe/Madrid', 'ibiza'), 'AGP': ('Europe/Madrid', 'malaga|málaga'), 'LIS': ('Europe/Lisbon', 'lisbon|lissabon'),
    'OPO': ('Europe/Lisbon', 'porto'), 'CPH': ('Europe/Copenhagen', 'copenhagen|kopenhagen'),
    'ARN': ('Europe/Stockholm', 'stockholm'), 'OSL': ('Europe/Oslo', 'oslo'), 'HEL': ('Europe/Helsinki', 'helsinki'),
    'KEF': ('Atlantic/Reykjavik', 'reykjavik|keflavik'), 'TLV': ('Asia/Jerusalem', 'tel aviv'),
    'CAI': ('Africa/Cairo', 'cairo|kairo'), 'JFK': ('America/New_York', 'new york|jfk'),
    'EWR': ('America/New_York', 'newark'), 'BOS': ('America/New_York', 'boston'),
    'SFO': ('America/Los_Angeles', 'san francisco'), 'LAX': ('America/Los_Angeles', 'los angeles'),
    'YUL': ('America/Toronto', 'montreal'), 'YYZ': ('America/Toronto', 'toronto'),
    'SIN': ('Asia/Singapore', 'singapore|singapur'), 'BKK': ('Asia/Bangkok', 'bangkok'),
    'HKG': ('Asia/Hong_Kong', 'hong kong|hongkong'), 'NRT': ('Asia/Tokyo', 'tokyo|narita'), 'HND': ('Asia/Tokyo', 'haneda'),
    'DPS': ('Asia/Makassar', 'bali|denpasar'), 'CMB': ('Asia/Colombo', 'colombo'),
    'NBO': ('Africa/Nairobi', 'nairobi'), 'ADD': ('Africa/Addis_Ababa', 'addis ababa'),
    'JNB': ('Africa/Johannesburg', 'johannesburg'), 'CPT': ('Africa/Johannesburg', 'cape town|kapstadt'),
}
# IATA airline -> ICAO (FlightAware's idents are ICAO: LX318 -> SWR318). Generic public data; extend as needed.
AIRLINES = {
    'EY': 'ETD', 'HM': 'SEY', 'AZ': 'ITY', 'LX': 'SWR', 'WK': 'EDW', 'EK': 'UAE', 'QR': 'QTR', 'TK': 'THY',
    'PC': 'PGT', 'LH': 'DLH', 'OS': 'AUA', 'SN': 'BEL', 'KL': 'KLM', 'AF': 'AFR', 'BA': 'BAW', 'U2': 'EZY',
    'DS': 'EZS', 'EC': 'EJU', 'FR': 'RYR', 'W6': 'WZZ', 'VY': 'VLG', 'IB': 'IBE', 'TP': 'TAP', 'JU': 'ASL',
    'A3': 'AEE', 'SK': 'SAS', 'AY': 'FIN', 'LO': 'LOT', 'EW': 'EWG', 'DE': 'CFG', 'X3': 'TUI', 'EI': 'EIN',
    'UA': 'UAL', 'DL': 'DAL', 'AA': 'AAL', 'AC': 'ACA', 'SQ': 'SIA', 'CX': 'CPA', 'KQ': 'KQA', 'ET': 'ETH',
    'MK': 'MAU', 'UL': 'ALK', 'FZ': 'FDB', 'G9': 'ABY', 'MS': 'MSR', 'LY': 'ELY', 'OU': 'CTN', 'BT': 'BTI',
    'HV': 'TRA', 'V7': 'VOE', 'NH': 'ANA', 'JL': 'JAL', 'TG': 'THA', 'SA': 'SAA',
}
EXTRA_F = os.environ.get('FLIGHTS_EXTRA') or os.path.expanduser('~/.config/flight-watch/airports.json')


def _load_extra():
    """Private additions (0600, outside the public code): {"airports": {"XXX": ["Area/Zone", "name|other name"]},
    "airlines": {"XX": "XXX"}}. A missing or broken file adds nothing."""
    try:
        with open(EXTRA_F) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return
    for code, v in ((d.get('airports') or {}) if isinstance(d, dict) else {}).items():
        if isinstance(v, list) and len(v) == 2 and re.fullmatch(r'[A-Z]{3}', str(code)):
            try:
                ZoneInfo(v[0])
            except (KeyError, ValueError):
                continue
            AIRPORTS[code] = (v[0], str(v[1]))
    for k, v in ((d.get('airlines') or {}) if isinstance(d, dict) else {}).items():
        if re.fullmatch(r'[A-Z0-9]{2}', str(k)) and re.fullmatch(r'[A-Z]{3}', str(v)):
            AIRLINES[k] = v


RAIL = {'RE', 'IR', 'IC', 'EC', 'EN', 'NJ', 'RJ', 'PE', 'SN', 'TGV', 'ICE', 'ICN', 'IRE', 'RB', 'RX'}
NO_RE = re.compile(r'^\s*(?:[-•*]\s*)?([A-Z][A-Z0-9]|[0-9][A-Z])\s?(\d{1,4})\b(.*)$')
TITLE_NO_RE = re.compile(r'\b([A-Z][A-Z0-9]|[0-9][A-Z])\s?(\d{1,4})\b')
ARROW_RE = re.compile(r'\s*(?:→|->|–>|=>)\s*')
WEEKDAY = (r'(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun|mo|di|mi|do|fr|sa|so|montag|dienstag|mittwoch|donnerstag|'
           r'freitag|samstag|sonntag|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\.?')
DATE_RE = re.compile(r'\b(\d{1,2})\.(\d{1,2})\.?(\d{4}|\d{2})?(?!\d)')
TIME_RE = re.compile(r'(?<![\d.:])(\d{1,2}):(\d{2})(?!\d)')
STOP_RE = re.compile(rf'(?i:\b{WEEKDAY}(?=[\s,]))|\d{{1,2}}\.\d{{1,2}}\.?|\d{{1,2}}:\d{{2}}')
CODE_RE = re.compile(r'\(([A-Z]{3})\)')


def fold(s):
    s = unicodedata.normalize('NFKD', str(s or '')).encode('ascii', 'ignore').decode().lower()
    return ' '.join(re.sub(r'[^a-z0-9 ]+', ' ', s).split())


_load_extra()
_ALIASES = sorted(((fold(a), code) for code, (_, names) in AIRPORTS.items() for a in names.split('|')),
                  key=lambda x: -len(x[0]))


def resolve(name):
    """'Doha (DOH)' -> 'DOH', 'Frankfurt' -> 'FRA', 'FRA' -> 'FRA', unknown -> None (a '(XXX)' code always counts)."""
    s = str(name or '')
    m = CODE_RE.search(s)
    if m:
        return m.group(1)
    for tok in re.findall(r'\b[A-Z]{3}\b', s):
        if tok in AIRPORTS:
            return tok
    f = fold(CODE_RE.sub('', s))
    if not f:
        return None
    for alias, code in _ALIASES:
        if f == alias or f.startswith(alias + ' ') or f.endswith(' ' + alias) or f' {alias} ' in f' {f} ':
            return code
    return None


def tz_of(code):
    a = AIRPORTS.get(str(code or '').upper())
    return ZoneInfo(a[0]) if a else None


def tz_name(code):
    a = AIRPORTS.get(str(code or '').upper())
    return a[0] if a else None


def last_sunday(year, month):
    d = dt.date(year, month + 1, 1) - dt.timedelta(days=1) if month < 12 else dt.date(year, 12, 31)
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)


def season(d):
    """IATA timetable season of a date: 'S2026' (last Sunday of March ..) or 'W2026' (last Sunday of October ..)."""
    if isinstance(d, dt.datetime):
        d = d.date()
    if d >= last_sunday(d.year, 10):
        return f'W{d.year}'
    if d >= last_sunday(d.year, 3):
        return f'S{d.year}'
    return f'W{d.year - 1}'


def icao_ident(no):
    """'LX318' -> 'SWR318' (unknown airline: unchanged)."""
    m = re.fullmatch(r'([A-Z0-9]{2})(\d+)', str(no or ''))
    return f'{AIRLINES[m.group(1)]}{m.group(2)}' if m and m.group(1) in AIRLINES else str(no or '')


def pretty(no):
    m = re.fullmatch(r'([A-Z0-9]{2})(\d+)', str(no or ''))
    return f'{m.group(1)} {m.group(2)}' if m else str(no or '')


def _side(text):
    """'Frankfurt Sat 14.11 16:10' -> (name, (day, month, year|None) or None, (h, m) or None)."""
    m = STOP_RE.search(text)
    name = (text[:m.start()] if m else text).strip(' ,;:-–(')
    name = re.sub(r'\s*\((?![A-Z]{3}\))[^)]*$', '', name).strip()
    dm, tm = DATE_RE.search(text), TIME_RE.search(text)
    date = None
    if dm:
        y = dm.group(3)
        date = (int(dm.group(1)), int(dm.group(2)), (int(y) + 2000 if y and len(y) == 2 else int(y)) if y else None)
    return name, date, ((int(tm.group(1)), int(tm.group(2))) if tm else None)


def _at(date, hm, zone):
    try:
        return dt.datetime(date.year, date.month, date.day, hm[0], hm[1], tzinfo=zone)
    except ValueError:
        return None


def _day(dmy, ref):
    """(d, m, y|None) -> a date: the given year, else the year that puts it closest after ref - 30 days."""
    d, m, y = dmy
    for year in ([y] if y else [ref.year, ref.year + 1, ref.year - 1]):
        try:
            x = dt.date(year, m, d)
        except ValueError:
            continue
        if y or x >= ref - dt.timedelta(days=30):
            return x
    return None


def parse_event(summary, desc, start, end):
    """Calendar ✈️ event -> legs [{no, from, to, dep, arr}] (see the module docstring); [] when nothing parses."""
    if not isinstance(start, dt.datetime):
        return []
    ev_tz = start.tzinfo
    end = end if isinstance(end, dt.datetime) and end > start else None
    raw = []
    for line in str(desc or '').splitlines():
        m = NO_RE.match(line)
        if not m or m.group(1) in RAIL:
            continue
        parts = ARROW_RE.split(m.group(3), maxsplit=1)
        if len(parts) != 2:
            continue
        (fa, fd, ft), (ta, td, tt) = _side(parts[0]), _side(parts[1])
        src, dst = resolve(fa), resolve(ta)
        if not src or not dst or src == dst:
            continue
        raw.append({'no': m.group(1) + m.group(2), 'from': src, 'to': dst, 'dd': fd, 'dt': ft, 'ad': td, 'at': tt})
    if not raw:
        title = str(summary or '')
        m = TITLE_NO_RE.search(title)
        body = ARROW_RE.split(re.sub(r'\([^)]*\)\s*$', '', title.replace('\u2708\ufe0f', '').replace('\u2708', '')),
                              maxsplit=1)
        if m and m.group(1) not in RAIL and len(body) == 2:
            src = resolve(TITLE_NO_RE.sub('', body[0]))
            dst = resolve(body[1])
            if src and dst and src != dst:
                raw.append({'no': m.group(1) + m.group(2), 'from': src, 'to': dst, 'dd': None, 'dt': None,
                            'ad': None, 'at': None})
    legs, prev = [], None
    for i, r in enumerate(raw):
        ztf, ztt = tz_of(r['from']) or ev_tz, tz_of(r['to']) or ev_tz
        if r['dt'] is None and i == 0:
            dep = start.astimezone(ztf)
        elif r['dt'] is None:
            dep = None
        else:
            ref = (prev.astimezone(ztf).date() if prev else start.astimezone(ztf).date())
            day = _day(r['dd'], ref) if r['dd'] else ref
            dep = _at(day, r['dt'], ztf) if day else None
            if dep and prev and not r['dd'] and dep < prev:
                dep += dt.timedelta(days=1)
        if dep is None:
            return legs                                   # a leg without a time: stop rather than guess
        if r['at'] is None:
            arr = end.astimezone(ztt) if (end and i == len(raw) - 1) else None
        else:
            day = _day(r['ad'], dep.astimezone(ztt).date()) if r['ad'] else dep.astimezone(ztt).date()
            arr = _at(day, r['at'], ztt) if day else None
            while arr and not r['ad'] and arr <= dep:
                arr += dt.timedelta(days=1)
        if arr is not None and arr <= dep:
            arr = None
        legs.append({'no': r['no'], 'from': r['from'], 'to': r['to'], 'dep': dep.isoformat(),
                     'arr': arr.isoformat() if arr else None})
        prev = arr or dep
    return legs
