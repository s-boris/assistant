"""livecount: Live Update countdown chips (status bar / Now Bar) in the owner's phone app, via `phone push --live`.

One chip per tag, updated in place (the gateway coalesces a tag's queued copies, the app re-posts the same
notification), ended with `phone cancel TAG` when it is no longer wanted. Watchers call `sync()` once per run, after
their main work, with the chips they want right now:

    import livecount
    livecount.sync('train', [{'key': leg_id, 'until': dep_datetime, 'title': 'Train 17:05', 'body': 'S9'}],
                   now=now, hold=True, ended=[cancelled_leg_id], test=simulated, dry=dry_run)

  key     the watcher's own id for the thing (hashed into the tag live:OWNER:<sha1[:10]>, so no ids leak)
  until   aware datetime or epoch seconds: the countdown's end (the chip shows its HH:MM)
  title   lock-screen safe (the push lint: <= 40 chars, body <= 80, no links, amounts, identifiers, 5+ digit runs);
          a chip that fails the lint is skipped (logged), never sent
  hold    chips not listed this run stay until their `until` passes (for watchers that stop looking at a thing
          before it is over, e.g. a delayed train after its scheduled time); without hold they are cancelled
  ended   keys to cancel now even with hold (a cancelled train, a cancelled flight)

Pushes go out at level info (the app's silent low-importance channel: an update never buzzes; the lock screen shows
only "Claude · 1 update", the chip shows the time). TTL = until + 5 min, so a queued copy never outlives its train.

Silent and cheap by design: no paired device (phone exit 4, status no_device) or a gateway that is down (exit 7)
backs off 10 min per watcher with no further phone call; each phone call has a 4 s timeout and one process spends
at most 5 s on phone calls in total; every error is swallowed and logged. Unchanged chips make no call at all.

Test mode (`test=True`, i.e. a simulated clock or test state) is a no-op unless LIVECOUNT_PHONE names a stub.
Env: LIVECOUNT_PHONE (the phone CLI; default ~/bin/phone), LIVECOUNT_DIR (state; default
~/.local/state/livecount), LIVECOUNT=off (no-op everywhere).
State: LIVECOUNT_DIR/<owner>.json (0600: tag -> until, title, body, pushed at; no-device backoff) + livecount.log.
"""
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time

HOME = os.path.expanduser('~')
CALL_TIMEOUT = 4.0           # seconds per phone call
BUDGET = 5.0                 # seconds of phone calls per process
BACKOFF = 600                # no device / gateway down: next try after this many seconds
GRACE = 120                  # an unlisted held chip is dropped this long after its until
TTL_PAD = 300
TITLE_MAX, BODY_MAX = 40, 80
DIGIT_RUN = re.compile(r"(?<!\d)\d(?:[ '’]?\d){4,}(?!\d)")
BAD = [(re.compile(r'(?i)\b(?:https?://|www\.)\S+'), 'link'), (re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+'), 'email'),
       (re.compile(r'(?i)\b(?:chf|fr\.|eur|usd)\s?\d|[€$]\s?\d'), 'amount'),
       (re.compile(r'(?<![\w])\+?\d[\d ()/-]{8,}\d'), 'phone'),
       (re.compile(r'(?i)\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}'), 'iban')]
_spent = 0.0


def _dir():
    return os.environ.get('LIVECOUNT_DIR') or os.path.join(HOME, '.local/state/livecount')


def _phone():
    return os.environ.get('LIVECOUNT_PHONE') or os.path.join(HOME, 'bin/phone')


def tag_for(owner, key):
    return 'live:%s:%s' % (re.sub(r'[^a-z0-9]', '', owner.lower())[:12] or 'x',
                           hashlib.sha1(str(key).encode()).hexdigest()[:10])


def epoch(t):
    if isinstance(t, (int, float)):
        return int(t)
    if isinstance(t, dt.datetime):
        if t.tzinfo is None:
            raise ValueError('livecount: naive datetime')
        return int(t.timestamp())
    raise ValueError('livecount: until must be a datetime or epoch')


def lint(title, body=''):
    """-> list of problems; empty = lock-screen safe (same limits as the push lint)."""
    out = []
    if not title:
        out.append('no title')
    if len(title) > TITLE_MAX:
        out.append('title > %d' % TITLE_MAX)
    if len(body) > BODY_MAX:
        out.append('body > %d' % BODY_MAX)
    for rx, name in BAD:
        if rx.search(title) or rx.search(body):
            out.append(name)
    if DIGIT_RUN.search(title) or DIGIT_RUN.search(body):
        out.append('digit run')
    return out


def _log(msg):
    try:
        d = _dir()
        os.makedirs(d, mode=0o700, exist_ok=True)
        path = os.path.join(d, 'livecount.log')
        if os.path.exists(path) and os.path.getsize(path) > 256 * 1024:
            os.replace(path, path + '.1')
        with open(path, 'a') as f:
            f.write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg))
    except OSError:
        pass


def _load(path):
    try:
        with open(path) as f:
            st = json.load(f)
        if isinstance(st, dict):
            st.setdefault('chips', {})
            return st
    except (OSError, ValueError):
        pass
    return {'chips': {}}


def _save(path, st):
    tmp = path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(st, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _call(argv):
    """-> (rc, parsed JSON or {}). rc None = not run (budget spent, no binary, timeout)."""
    global _spent
    left = BUDGET - _spent
    if left <= 0.2:
        return None, {'error': 'budget'}
    if not os.access(argv[0], os.X_OK):
        return None, {'error': 'no phone binary'}
    t0 = time.monotonic()
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=min(CALL_TIMEOUT, left),
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return None, {'error': type(e).__name__}
    finally:
        _spent += time.monotonic() - t0
    try:
        j = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
    except (ValueError, IndexError):
        j = {}
    return r.returncode, j if isinstance(j, dict) else {}


def _push(tag, c, now_s):
    ttl = max(120, min(86400, c['until'] - now_s + TTL_PAD))
    return _call([_phone(), 'push', '--level', 'info', '--title', c['title'], '--body', c['body'], '--tag', tag,
                  '--live', 'countdown:%d' % c['until'], '--ttl', '%ds' % ttl, '--json'])


def _cancel(tag):
    return _call([_phone(), 'cancel', tag, '--json'])


def sync(owner, items, now=None, hold=False, ended=(), test=False, dry=False):
    """Bring this watcher's chips in line with `items`. -> list of action lines (for --dry-run output).
    Never raises."""
    try:
        return _sync(owner, items, now, hold, ended, test, dry)
    except Exception as e:                                   # a chip must never break its watcher
        _log('%s: error %s: %s' % (owner, type(e).__name__, str(e)[:160]))
        return ['live: error %s' % type(e).__name__]


def _sync(owner, items, now, hold, ended, test, dry):
    if (os.environ.get('LIVECOUNT') or '').lower() in ('off', '0', 'no'):
        return []
    if test and not os.environ.get('LIVECOUNT_PHONE') and not dry:
        return []
    now_s = epoch(now) if now is not None else int(time.time())
    want, acts = {}, []
    for it in items or []:
        c = {'until': epoch(it['until']), 'title': str(it.get('title') or '').strip(),
             'body': str(it.get('body') or '').strip()}
        if c['until'] <= now_s:
            continue
        bad = lint(c['title'], c['body'])
        if bad and c['body'] and not lint(c['title']):        # keep the chip, drop the detail line
            _log('%s: %s body dropped (lint: %s)' % (owner, tag_for(owner, it['key']), ', '.join(bad)))
            c['body'], bad = '', []
        if bad:
            acts.append('live: skip %s (lint: %s)' % (it['key'], ', '.join(bad)))
            _log('%s: skip %s (lint: %s)' % (owner, tag_for(owner, it['key']), ', '.join(bad)))
            continue
        want[tag_for(owner, it['key'])] = c
    end_tags = {tag_for(owner, k) for k in ended or ()}
    d = _dir()
    path = os.path.join(d, re.sub(r'[^a-z0-9-]', '', owner.lower()) + '.json')
    if dry:
        st = _load(path)
        for tag, c in want.items():
            old = st['chips'].get(tag)
            if not old or any(old.get(k) != c[k] for k in ('until', 'title', 'body')):
                acts.append('live: %s chip "%s" (%s) until %s' % ('update' if old else 'start', c['title'], c['body'],
                                                                 time.strftime('%H:%M', time.localtime(c['until']))))
        for tag in st['chips']:
            if tag not in want and (not hold or tag in end_tags or st['chips'][tag]['until'] + GRACE < now_s):
                acts.append('live: end chip %s' % tag)
        return acts
    if not want and not os.path.exists(path):
        return acts                                          # the cheap path: nothing live, nothing to end
    os.makedirs(d, mode=0o700, exist_ok=True)
    lock = os.open(path + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return acts                                      # another run of this watcher: next run catches up
        st = _load(path)
        chips = st['chips']
        changed = False
        backoff = int(st.get('backoff_until') or 0) > now_s
        for tag in list(chips):
            c = chips[tag]
            if tag in want:
                continue
            if hold and tag not in end_tags and c.get('until', 0) + GRACE >= now_s:
                continue
            rc, _ = (None, {}) if backoff else _cancel(tag)
            if rc == 0 or c.get('until', 0) + GRACE < now_s:
                chips.pop(tag)                               # gone, or the app times it out by itself anyway
                changed = True
                acts.append('live: end %s' % tag)
                _log('%s: end %s rc=%s' % (owner, tag, rc))
        for tag, c in want.items():
            old = chips.get(tag)
            if old and all(old.get(k) == c[k] for k in ('until', 'title', 'body')):
                continue
            if backoff:
                break
            rc, j = _push(tag, c, now_s)
            status = j.get('status')
            _log('%s: push %s until %d rc=%s %s' % (owner, tag, c['until'], rc, status or j.get('error') or ''))
            if rc == 0 or rc == 4 and status == 'queued':
                chips[tag] = dict(c, at=now_s)
                changed = True
                acts.append('live: %s %s' % ('update' if old else 'start', tag))
            elif rc == 4 or rc == 7:                         # no paired device / gateway down: quiet for a while
                st['backoff_until'] = now_s + BACKOFF
                changed = True
                acts.append('live: no device (%s), back off' % (status or rc))
                break
            elif rc == 3:                                    # the gateway's lint said no: don't retry this text
                chips[tag] = dict(c, at=now_s, refused='lint')
                changed = True
            else:
                break                                        # timeout / budget / usage: next run retries
        if not chips and int(st.get('backoff_until') or 0) <= now_s:
            try:
                os.remove(path)                              # nothing live: the next run takes the cheap path
            except OSError:
                pass
        elif changed:
            _save(path, st)
        return acts
    finally:
        os.close(lock)
