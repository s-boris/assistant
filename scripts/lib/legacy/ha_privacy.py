"""ha_privacy: the privacy gate for Home Assistant data before it reaches stdout, logs or my context.

Used by ~/bin/ha (not vendored by the vault, so importing bin/lib is fine). The scrub regexes are
copied unchanged from ~/bin/ha-audit (ha-audit itself is not touched); ~/opt/ha-tests checks that the
two copies stay equal.

Two layers, both always on:
  1. gate_state(): structured. Drops location/address/token-like attribute keys at any nesting depth,
     masks SSID/BSSID/MAC keys, hides location values (device_tracker/person/zone/proximity/geocoded and
     other location-ish sensors show only a zone name or home/not_home/unavailable), masks Bluetooth
     tracker names, turns coordinate-looking floats into <coord>.
  2. text_gate(): every printed line is re-scrubbed (emails, MACs, IPs, coordinates, street addresses,
     postcode+town) and the API token is replaced, whatever the caller printed.
"""
import hashlib
import re
import unicodedata

# ----------------------------------------------------------------------------- regexes (= ha-audit)
EMAIL_RE = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+')
EMAIL_SLUG_RE = re.compile(r'(?<![a-z0-9])[a-z0-9]+(?:_[a-z0-9]+)*?_(?:gmail|googlemail|outlook|hotmail|live|yahoo|icloud|'
                           r'gmx|bluewin|protonmail|proton|hispeed|sunrise)_(?:com|ch|de|net|me|li)(?![a-z0-9])', re.I)
MAC_RE = re.compile(r'(?<![0-9A-Za-z])[0-9A-Fa-f]{2}(?:[:\-_][0-9A-Fa-f]{2}){5}(?![0-9A-Za-z])')
MAC12_RE = re.compile(r'(?<![0-9A-Za-z])(?=[0-9]{0,11}[A-Fa-f])[0-9A-Fa-f]{12}(?![0-9A-Za-z])')
IP4_RE = re.compile(r'(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?![\d.])')
IP6_RE = re.compile(r'(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{1,4}:){3,7}[0-9A-Fa-f]{1,4}(?![0-9A-Fa-f:])')
IP6C_RE = re.compile(r'(?<![0-9A-Fa-f:])(?=[0-9A-Fa-f:]*[0-9A-Fa-f])(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*)?::'
                     r'(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*)?(?![0-9A-Fa-f:])')
COORD_RE = re.compile(r'(?<![\w.])-?\d{1,3}\.\d{4,}(?![\w.])')
STREET_RE = re.compile(r'\b[\wÄÖÜäöüß\-]*(?:strasse|straße|str\.|gasse|weg|platz|allee)\s+\d+[a-z]?\b', re.I)
PLZ_RE = re.compile(r'\b(?:CH-)?[1-9]\d{3}\s+[A-ZÄÖÜ][a-zäöü]{2,}')
SENSITIVE_OBJ_RE = re.compile(
    r'geocod|location|wifi|ssid|bssid|bluetooth|ble_|beacon|(?:^|_)ip(?:_|$)|ip_address|public_ip|'
    r'notification|media|activity|address|gps|latitude|longitude|altitude|coordinat|place|travel|'
    r'distance|direction|proximity|zone|network|carrier|connection', re.I)
# ----------------------------------------------------------------------------- end of ha-audit copy

# extra phone-sensor content that is nobody's business in a transcript
EXTRA_SENSITIVE_OBJ_RE = re.compile(r'last_used_app|current_app|clipboard|phone_state|last_call|sms|'
                                    r'last_update_trigger|keyguard|beacon_monitor', re.I)
TS_FRACTION_RE = re.compile(r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})\.\d+')
URL_SECRET_RE = re.compile(r'([?&;](?:[a-z_]*token|api_?key|apikey|key|auth|sig|signature|password|pass|secret)=)'
                           r'[^&\s"\']+', re.I)

LOCATION_DOMAINS = {'device_tracker', 'person', 'proximity', 'geo_location'}
# domains whose values are measurements/text: here an entity named like a location/network/notification
# sensor gets its value hidden (control domains such as media_player keep theirs)
VALUE_DOMAINS = {'sensor', 'binary_sensor', 'text', 'input_text', 'event', 'notify', 'image', 'select',
                 'input_select', 'number', 'input_number', 'todo', 'tag'}
NO_ATTR_DOMAINS = {'calendar', 'camera', 'image', 'tts', 'stt', 'conversation', 'notify', 'event', 'todo'}
SAFE_STATES = {'on', 'off', 'home', 'not_home', 'unavailable', 'unknown', 'true', 'false'}
BT_PLATFORMS = ('ibeacon', 'private_ble_device', 'bluetooth_le_tracker', 'bluetooth')

# attribute keys dropped at any depth (location, addresses, secrets/tokens, pictures with tokens)
DROP_KEY_RE = re.compile(
    r'latitude|longitude|^lat$|^lon$|^lng$|gps|altitude|location|address|street|thoroughfare|postal|zip|'
    r'locality|^city$|country|geocod|coordinat|^place|premises|area_of_interest|areas_of_interest|feature_name|'
    r'entity_picture|picture|token|password|passwd|secret|api_?key|^ip$|(?:^|_)ip_|^host(?:name)?$|'
    r'^in_zones$|^android\.|^notification|^message$|^description$|current_address|^uuid$|^major$|^minor$',
    re.I)
SSID_KEY_RE = re.compile(r'ssid|wifi|essid|network_name|^network$|access_point', re.I)
MAC_KEY_RE = re.compile(r'(?:^|_)mac(?:_|$)|bssid|bluetooth_address|bt_addr', re.I)
# floats under these keys are ordinary numbers (rounded); any other float with 4+ decimals in the range
# of a coordinate is masked, because a coordinate can hide under an innocent key
SAFE_NUM_KEY_RE = re.compile(
    r'volume|brightness|color_temp|kelvin|temperature|temp_|humidity|percentage|position|media_duration|'
    r'battery|hs_color|xy_color|rgb|power|energy|current|voltage|speed|level|^min_|^max_|step|'
    r'target_temp|supported_features|duration|progress|offset', re.I)
REDACTION_TAGS = ('<email>', '<mac>', '<ip>', '<coord>', '<address>', '<ssid>', '<token>')


def scrub(s, names=False):
    """= ha-audit's scrub(): emails, MACs, IPs, coordinates, street addresses (+ postcode/town)."""
    if s is None:
        return None
    s = str(s)
    s = EMAIL_RE.sub('<email>', s)
    s = EMAIL_SLUG_RE.sub('<email>', s)
    s = MAC_RE.sub('<mac>', s)
    s = MAC12_RE.sub('<mac>', s)
    s = IP4_RE.sub('<ip>', s)
    s = IP6_RE.sub('<ip>', s)
    s = IP6C_RE.sub('<ip>', s)
    s = COORD_RE.sub('<coord>', s)
    s = STREET_RE.sub('<address>', s)
    if names:
        s = PLZ_RE.sub('<address>', s)
    return s


def text_gate(text, secrets=()):
    """Final layer for every line that is printed or logged."""
    s = str(text)
    for sec in secrets:
        if sec:
            s = s.replace(sec, '<token>')
    return clean(s)


def clean(s):
    """scrub() for one value: timestamps lose their fraction first (12:34:56.789 is no coordinate),
    URL query secrets are masked."""
    if s is None:
        return None
    s = URL_SECRET_RE.sub(r'\1<token>', TS_FRACTION_RE.sub(r'\1', str(s)))
    return scrub(s, names=True)


def norm(s):
    """lowercase, accents folded, anything else -> single spaces (for matching)."""
    s = unicodedata.normalize('NFKD', str(s or '')).encode('ascii', 'ignore').decode().lower()
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', s).split())


def slugify(s):
    return norm(s).replace(' ', '_')


def bt_mask(eid, platform=None):
    dom = eid.split('.', 1)[0]
    return f"{dom}.[bt:{platform or 'bluetooth'}:{hashlib.sha1(eid.encode()).hexdigest()[:6]}]"


def is_bt(eid, attrs, bt_ids=()):
    if eid in bt_ids:
        return True
    dom, _, obj = eid.partition('.')
    a = attrs or {}
    if dom in ('device_tracker', 'sensor') and (obj.startswith('ble_') or
                                                ('uuid' in a and ('major' in a or 'minor' in a)) or
                                                'current_address' in a):
        return True
    return False


def is_sensitive_value(eid, friendly=''):
    """A value-type entity whose value is location/network/notification-ish."""
    dom, _, obj = eid.partition('.')
    if dom not in VALUE_DOMAINS:
        return False
    hay = obj + ' ' + slugify(friendly)
    return bool(SENSITIVE_OBJ_RE.search(hay) or EXTRA_SENSITIVE_OBJ_RE.search(hay))


def _fmt_float(v):
    r = round(v, 3)
    return int(r) if r == int(r) and abs(r) < 1e15 else r


def gate_value(v, key=''):
    """Recursive attribute gate: drop/mask by key, mask coordinate-like floats, scrub strings."""
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            ks = str(k)
            if DROP_KEY_RE.search(ks):
                continue
            if MAC_KEY_RE.search(ks):
                out[ks] = '<mac>' if x not in (None, '', [], {}) else x
                continue
            if SSID_KEY_RE.search(ks):
                out[ks] = '<ssid>' if x not in (None, '', [], {}) else x
                continue
            out[ks] = gate_value(x, ks)
        return out
    if isinstance(v, (list, tuple)):
        return [gate_value(x, key) for x in v]
    if isinstance(v, bool) or v is None or isinstance(v, int):
        return v
    if isinstance(v, float):
        if SAFE_NUM_KEY_RE.search(key or ''):
            return _fmt_float(v)
        if COORD_RE.fullmatch(repr(v)) and abs(v) <= 180:
            return '<coord>'
        return _fmt_float(v)
    return clean(v)


def _round_state(state, attrs):
    """Long decimals of a measured value (21.53125 °C) -> 2 decimals, so the text gate does not
    mistake them for coordinates. Only with a unit that is not degrees of arc."""
    unit = str((attrs or {}).get('unit_of_measurement') or '')
    if not unit or unit == '°':
        return state
    try:
        f = float(state)
    except (TypeError, ValueError):
        return state
    if '.' in str(state) and len(str(state).split('.', 1)[1]) > 2:
        return f'{f:.2f}'
    return state


def gate_state(s, zone_names=(), bt_ids=(), area=None):
    """One HA state object -> a privacy-gated dict:
    {entity_id, name, state, area, attributes, hidden: reason|None}."""
    eid = str(s.get('entity_id', ''))
    dom = eid.split('.', 1)[0]
    attrs = s.get('attributes') or {}
    raw_state = s.get('state')
    friendly = attrs.get('friendly_name') or ''
    out = {'entity_id': eid, 'name': clean(friendly) if friendly else '', 'state': raw_state,
           'area': clean(area) if area else None, 'attributes': {}, 'hidden': None}
    if is_bt(eid, attrs, bt_ids):
        out.update(entity_id=bt_mask(eid), name='Bluetooth device', hidden='bluetooth')
        out['state'] = raw_state if raw_state in SAFE_STATES else '<hidden>'
        return out
    zones = {str(z).lower() for z in zone_names if z}
    keep_attrs = {'friendly_name'}
    if dom in LOCATION_DOMAINS:
        st = str(raw_state)
        if st in SAFE_STATES or st.lower() in zones:
            out['state'] = clean(st)
        else:
            out['state'] = 'not_home' if dom in ('device_tracker', 'person') else '<hidden>'
        out['hidden'] = 'location'
    elif dom == 'zone':
        out['state'] = '<hidden>'
        out['hidden'] = 'location'
    elif is_sensitive_value(eid, friendly):
        out['state'] = raw_state if str(raw_state) in SAFE_STATES else '<hidden>'
        out['hidden'] = 'location/network/notification'
        keep_attrs = {'friendly_name', 'unit_of_measurement', 'device_class', 'icon', 'state_class'}
    elif dom in NO_ATTR_DOMAINS:
        out['state'] = clean(raw_state)
        out['hidden'] = 'content'
    else:
        out['state'] = clean(_round_state(raw_state, attrs))
        out['attributes'] = gate_value({k: v for k, v in attrs.items() if k != 'friendly_name'})
        return out
    out['attributes'] = gate_value({k: v for k, v in attrs.items() if k in keep_attrs and k != 'friendly_name'})
    return out
