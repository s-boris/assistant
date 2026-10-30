"""catstock: the cat supplies stock ledger shared by cat-autopilot and cat-supplies (local file, no network).

File: $CAT_CORNER_STATE_DIR/stock.json (default ~/.local/state/cat-corner/stock.json, 0600, written under a lock):
  {"set": {KEY: {"qty", "ts", "src", "exact"}},          his answer: the count at that moment
   "events": [{"id", "ts", "key", "kind", "delta", "src"}],  use after the answer (button / HA reset / top-up)
   "incoming": [{"id", "ts", "eta", "shop", "key", "qty"}],  ordered packs: count as stock from the ETA on
   "answers": {...}, "seq": N}                               non-count answers; seq orders same-second writes
Stock now = set.qty + the deltas after set.ts + incoming arrived after set.ts (never below 0).
A key with no answer falls back to cat-corner config "supplies"."stock" (no events applied), else unknown.
"""
import fcntl
import json
import os
import time

KEYS = {'litter_kg': 'kg', 'dry_food_kg': 'kg', 'drawer_bags': 'bags', 'fountain_filters': 'filters',
        'desiccant_packs': 'packs', 'feeder_batteries': 'cells', 'wet_portions': 'portions'}
DEDUP_SEC = 48 * 3600          # a button tap and an HA-seen reset of the same kind this close = one change
MAX_EVENTS = 300


def state_dir():
    return os.environ.get('CAT_CORNER_STATE_DIR') or os.path.expanduser('~/.local/state/cat-corner')


def path():
    return os.path.join(state_dir(), 'stock.json')


def empty():
    return {'set': {}, 'events': [], 'incoming': [], 'answers': {}}


def load():
    try:
        with open(path()) as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return empty()
    if not isinstance(d, dict):
        return empty()
    for k, v in empty().items():
        if not isinstance(d.get(k), type(v)):
            d[k] = v
    return d


def write_json(p, obj):
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    tmp = f'{p}.{os.getpid()}.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


class Ledger:
    """with Ledger() as d: ... -> saved on a clean exit (under an exclusive lock)."""
    def __enter__(self):
        os.makedirs(state_dir(), mode=0o700, exist_ok=True)
        self._lk = open(os.path.join(state_dir(), 'stock.lock'), 'a')
        fcntl.flock(self._lk, fcntl.LOCK_EX)
        self.d = load()
        return self.d

    def __exit__(self, et, ev, tb):
        try:
            if et is None:
                self.d['events'] = sorted(self.d['events'], key=lambda e: e.get('ts', 0))[-MAX_EVENTS:]
                write_json(path(), self.d)
        finally:
            fcntl.flock(self._lk, fcntl.LOCK_UN)
            self._lk.close()
        return False


def _seq(d):
    d['seq'] = int(d.get('seq') or 0) + 1
    return d['seq']


def set_qty(d, key, qty, src='answer', ts=None, exact=True):
    if key not in KEYS:
        raise ValueError(f'unknown stock key {key}')
    d['set'][key] = {'qty': max(float(qty), 0.0), 'ts': float(ts or time.time()), 'src': src, 'exact': bool(exact),
                     'seq': _seq(d)}


def add_event(d, key, delta, kind, src, ts=None, eid=None):
    """Record a use (delta < 0) or a correction. HA-seen and tapped changes of one kind within 48 h count once.
    Returns 'added', 'dup' (same id) or 'merged' (the other source already counted it)."""
    ts = float(ts or time.time())
    eid = eid or f'{src}:{kind}:{int(ts)}'
    ev = d['events']
    if any(e.get('id') == eid for e in ev):
        return 'dup'
    other = [e for e in ev if e.get('kind') == kind and e.get('src') != src and abs(e.get('ts', 0) - ts) < DEDUP_SEC]
    if other and src in ('ha', 'button'):
        return 'merged'
    ev.append({'id': eid, 'ts': ts, 'key': key, 'kind': kind, 'delta': float(delta), 'src': src, 'seq': _seq(d)})
    return 'added'


def add_incoming(d, shop, lines, eta_ts, ts=None):
    ts = float(ts or time.time())
    for key, qty in lines:
        if key in KEYS and qty:
            d['incoming'].append({'id': f'{shop}:{key}:{int(ts)}', 'ts': ts, 'eta': float(eta_ts), 'shop': shop,
                                  'key': key, 'qty': float(qty), 'seq': _seq(d)})


def current(d, now=None, fallback=None):
    """{key: {qty, src, exact, at, used, arrived, pending}} for every key with an answer (or a config fallback)."""
    now = float(now or time.time())
    out = {}
    for key, s in d.get('set', {}).items():
        if key not in KEYS or not isinstance(s, dict):
            continue
        t0, q0 = s.get('ts', 0), s.get('seq', 0)
        after = lambda e: (e.get('ts', 0), e.get('seq', 0)) > (t0, q0)      # same second: recorded after the answer
        used = sum(e['delta'] for e in d['events'] if e.get('key') == key and after(e) and e.get('ts', 0) <= now)
        arrived = sum(i['qty'] for i in d['incoming'] if i.get('key') == key and after(i) and i.get('eta', 0) <= now)
        pending = sum(i['qty'] for i in d['incoming'] if i.get('key') == key and i.get('eta', 0) > now)
        out[key] = {'qty': round(max(s['qty'] + used + arrived, 0.0), 2), 'src': s.get('src', 'answer'),
                    'exact': s.get('exact', True), 'at': t0, 'used': round(-used, 2), 'arrived': arrived,
                    'pending': pending}
    for key, v in (fallback or {}).items():
        if key in KEYS and key not in out and isinstance(v, (int, float)):
            out[key] = {'qty': float(v), 'src': 'config', 'exact': True, 'at': 0, 'used': 0, 'arrived': 0,
                        'pending': 0}
    return out


def values(fallback=None, now=None):
    """{key: qty} (for cat-supplies): ledger stock over the config's "supplies"."stock"."""
    return {k: v['qty'] for k, v in current(load(), now, fallback).items()}
