"""night-plan settime: watch event pages for a published set time, then pick the train home. Token-free (cron).

  night-plan settime add EVENT_REF URL [URL ...] [--artist NAME] [--start HH:MM] [--set-min 120] [--exit-min 20]
                         [--until "YYYY-MM-DD HH:MM"]
  night-plan settime add --venue "Venue, Town" --date YYYY-MM-DD --artist NAME [--title T] URL [URL ...] [...]
  night-plan settime list [--json]           watches, their sources and state
  night-plan settime rm ID                   stop a watch
  night-plan settime check [ID] [--cron] [--dry-run] [--force]
  night-plan settime pick ID HH:MM[-HH:MM]   the note for a set time read by hand (printed; nothing saved or written)

EVENT_REF  a rave-radar event id (unique prefix >= 4), as for night-plan: title, artist, venue, date come from there.
Sources    the venue's, promoter's and artist's own pages (any http(s) URL). Fetching follows watch's rules (imports
           ~/bin/watch: its User-Agent, robots.txt, refused hosts such as ra.co and Meta, bot walls never bypassed),
           at least 10 min between requests to one host, conditional GETs.
Parse      the visible text: the artist's name (accents/case ignored; the last name alone when >= 5 letters) right
           next to a time or a range ("Name 01:00-03:00", "01:00 - 03:00 | Name", or on the adjacent line), with only
           separators in between (no words, no date). Times on both sides: the layout around the name decides
           (time-first or name-first). Times before the event start - 2 h are the next day; a single time equal to the
           event start is ignored (doors); end = the range end, else start + --set-min (said to be assumed).
           A new "timetable / set times / running order / Zeitplan" mention near the name (or a new <img> naming one
           on a page whose title has the name) without a readable time -> one "spotted" note; it keeps watching.
Pick       `night-plan EVENT --start S --end SET_END --exit-min X --json`: the train that fits = the first way home
           leaving after the set end + X min (+ the walk to the stop); plus last back / first morning, and the
           calendar's current leg home (calcache: a train/bus leg to home starting within 16 h after the event start).
           Draft calendar change = that leg moved to the fitting connection (create_event fields when there is none).
Note       ONE ~/inbox/<last name>-settime-<epoch>.md (0600) when the set time is found: set, end, the train that fits,
           the alternatives, the current leg, the draft change (JSON). Then the watch is "found" and stops. Never
           messages anyone, never writes the calendar.
Schedule   due every 3 h, hourly from 3 days before the start, every 30 min from 24 h before; ends at --until (default
           start + 4 h; then silently "expired"). Every source failing 6 checks in a row -> one
           <last name>-settime-failed-<epoch>.md note.
State      ~/.local/state/night-plan/settime.json (0600, locked). Env (tests): NIGHT_SETTIME_STATE_DIR,
           NIGHT_SETTIME_INBOX, NIGHT_SETTIME_NOW ("YYYY-MM-DD HH:MM" or epoch), NIGHT_SETTIME_PLAN (night-plan
           binary), NIGHT_SETTIME_CALCACHE (a `calcache show --json` file, or "off"), NIGHT_SETTIME_WATCH (watch
           script), NIGHT_SETTIME_HOST_GAP (seconds), RAVE_RADAR_STATE_DIR.
Exit: 0 ok, 1 state or plan error, 2 usage error. Tests: ~/opt/night-plan-tests/test_settime.py
"""
import argparse
import datetime as dt
import difflib
import fcntl
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unicodedata

LIB = os.path.dirname(os.path.realpath(__file__))
if LIB not in sys.path:
    sys.path.insert(0, LIB)
import assistant_profile as profile  # noqa: E402
import botlib  # noqa: E402

HOME = os.path.expanduser('~')
STATE_DIR = os.environ.get('NIGHT_SETTIME_STATE_DIR') or f'{HOME}/.local/state/night-plan'
STATE_F = os.path.join(STATE_DIR, 'settime.json')
INBOX = os.environ.get('NIGHT_SETTIME_INBOX') or f'{HOME}/inbox'
RR_STATE = os.path.join(os.environ.get('RAVE_RADAR_STATE_DIR') or f'{HOME}/.local/state/rave-radar', 'state.json')
HOST_GAP = int(os.environ.get('NIGHT_SETTIME_HOST_GAP', '600'))
FAIL_NOTE_AFTER, SEP_MAX, WINDOW = 6, 14, 800
TZ = profile.tz()
HOUR = 3600

KW = re.compile(r'time ?table|set ?times?|running ?order|zeitplan|spielzeiten|line-?up times|time ?slots?')
T = r'(?<![\d.:])(?:[01]?\d|2[0-3])(?:\s?[:h]\s?|\.)[0-5]\d(?!\d|[.:]\d)(?:\s?(?:uhr|h)(?![a-z]))?'
TOK = re.compile(rf'(?P<a>{T})(?:\s*(?:-|–|—|bis|to|till|until)\s*(?P<b>{T}))?')
NAME_FIRST = re.compile(r'[a-z][\s|:\-\u2013\u2014/(]{0,8}$')
TIME_FIRST = re.compile(r'[\s|:\-–—/]{0,8}[a-z]')
SEP = re.compile(r'[\s|:·•\-–—/()\[\]*,>@]*')


class Usage(Exception):
    pass


class Perm(Exception):
    """A source that must not be fetched (robots.txt, refused host, bot wall)."""


# ---------------------------------------------------------------- basics

def now():
    v = os.environ.get('NIGHT_SETTIME_NOW')
    if not v:
        return time.time()
    if re.fullmatch(r'\d+(\.\d+)?', v):
        return float(v)
    return dt.datetime.strptime(v, '%Y-%m-%d %H:%M').replace(tzinfo=TZ).timestamp()


def local(ts):
    return dt.datetime.fromtimestamp(ts, TZ)


def hm(d):
    return d.strftime('%H:%M')


def day_hm(d):
    return d.strftime('%a %d.%m %H:%M')


def iso(s):
    d = dt.datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=TZ)


def fold(s):
    """Lower-case ASCII fold that keeps the length (positions in the folded text = positions in the original)."""
    out = []
    for c in s:
        a = unicodedata.normalize('NFKD', c).encode('ascii', 'ignore').decode()[:1] or c
        lo = a.lower()
        out.append(lo if len(lo) == 1 else a)
    return ''.join(out)


def slug(artist):
    w = re.findall(r'[a-z0-9]+', fold(artist or ''))
    return w[-1] if w else 'event'


_W = None


def W():
    """watch's fetch rules (User-Agent, robots.txt, refused hosts, bot walls), imported from the watch script."""
    global _W
    if _W is None:
        path = os.environ.get('NIGHT_SETTIME_WATCH') or os.path.join(os.path.dirname(LIB), 'watch')
        loader = importlib.machinery.SourceFileLoader('watch_rules', path)
        spec = importlib.util.spec_from_loader('watch_rules', loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        _W = mod
    return _W


class Locked:
    def __enter__(self):
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        self.f = open(os.path.join(STATE_DIR, '.settime.lock'), 'w')
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        self.f.close()


def load():
    st = botlib.load_json(STATE_F, None)
    if not isinstance(st, dict):
        st = {}
    st.setdefault('watches', {})
    st.setdefault('hosts', {})
    return st


def save(st):
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    botlib.save_json(STATE_F, st, indent=1, ensure_ascii=False)
    os.chmod(STATE_F, 0o600)


def write_note(prefix, text):
    os.makedirs(INBOX, exist_ok=True)
    f = botlib.write_inbox(INBOX, prefix, text)
    os.chmod(f, 0o600)
    return f


# ---------------------------------------------------------------- event + calendar

def rave_event(ref):
    evs = (botlib.load_json(RR_STATE, {}) or {}).get('events') or {}
    if ref in evs:
        return evs[ref]
    hits = [e for k, e in evs.items() if len(ref) >= 4 and k.startswith(ref)]
    if len(hits) == 1:
        return hits[0]
    raise Usage(f'unknown rave-radar event {ref!r}' if not hits else f'{ref!r} matches {len(hits)} events')


def calendar_events():
    src = os.environ.get('NIGHT_SETTIME_CALCACHE')
    if src == 'off':
        return None
    try:
        if src:
            with open(src) as f:
                d = json.load(f)
        else:
            p = subprocess.run(['calcache', 'show', '--days', '21', '--json'], capture_output=True, text=True,
                               timeout=60)
            if p.returncode != 0:
                return None
            d = json.loads(p.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return d if isinstance(d, list) else None


def similar(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.7


def cal_event(w, events):
    """The calendar's own event for this night (timed, not travel, that date, same town or a name like the artist's)."""
    last = slug(w['artist'])
    for e in events or []:
        if e.get('all_day') or (e.get('flags') or {}).get('travel') or not str(e.get('start', '')).startswith(w['date']):
            continue
        title_words = re.findall(r'[a-z0-9]+', fold(e.get('title') or ''))
        if fold(e.get('town') or '') == fold(w.get('town') or '-') or any(similar(x, last) for x in title_words):
            return e
    return None


def cal_leg_home(start, events):
    legs = []
    for e in events or []:
        tr = e.get('train') or {}
        if (e.get('flags') or {}).get('travel') not in ('train', 'bus') or tr.get('to') != 'home':
            continue
        s = iso(e['start'])
        if start <= s <= start + dt.timedelta(hours=16):
            legs.append((s, e))
    return min(legs, key=lambda x: x[0])[1] if legs else None


# ---------------------------------------------------------------- parsing

def artist_re(artist):
    toks = re.findall(r'[a-z0-9]+', fold(artist))
    if not toks:
        raise Usage('--artist is empty')
    alts = [r'[\s\-_.]*'.join(map(re.escape, toks))]
    if len(toks) > 1 and len(toks[-1]) >= 5:
        alts.append(re.escape(toks[-1]))
    return re.compile(r'(?<![a-z0-9])(?:' + '|'.join(alts) + r')(?![a-z0-9])')


def tok_ok(m):
    """A '.' time (01.00) counts only with 'Uhr'/'h' or as part of a range (dates like 19.10 do not)."""
    a, b = m.group('a'), m.group('b')
    return b is not None or not re.fullmatch(r'\d{1,2}\.\d\d', a.strip())


def sep_ok(s):
    return len(s) <= SEP_MAX and s.count('\n') <= 1 and SEP.fullmatch(s) is not None


def clock(s):
    h, m = re.match(r'(\d{1,2})\s?[:h.]\s?(\d\d)', s).groups()
    return int(h), int(m)


def page_text(html_text):
    p = W().PageText()
    try:
        p.feed(html_text)
        p.close()
    except Exception:
        pass
    text, title, _ = p.result()
    return text, title


def parse(html_text, artist, start):
    """-> (candidates [{start, end, assumed_end?, snippet}], keyword contexts set, mentions count)."""
    text, title = page_text(html_text)
    ft = fold(text)
    are = artist_re(artist)
    toks = [m for m in TOK.finditer(ft) if tok_ok(m)]
    cands, kws, mentions = [], set(), 0
    for am in are.finditer(ft):
        mentions += 1
        s, e = am.span()
        after = None
        m = SEP.match(ft, e)
        if m and sep_ok(m.group(0)):
            nxt = TOK.match(ft, m.end())
            if nxt and tok_ok(nxt):
                after = nxt
        before = None
        for t in toks:
            if t.end() <= s and sep_ok(ft[t.end():s]):
                before = t
        if after and before:
            sb, sa = ft[before.end():s], ft[e:after.start()]
            lo, hi = max(0, s - 600), e + 600
            tf = sum(1 for t in toks if lo <= t.start() < hi and TIME_FIRST.match(ft, t.end()))
            nf = sum(1 for t in toks if lo <= t.start() < hi and NAME_FIRST.search(ft[max(0, t.start() - 10):t.start()]))
            if strong(sb) != strong(sa):             # "01:00 Name | 03:00 Next": the weak separator binds
                pick = after if strong(sb) else before
            elif ('\n' in sb) != ('\n' in sa):       # same line beats the adjacent line
                pick = after if '\n' in sb else before
            elif tf != nf:                           # the page's layout around the name
                pick = before if tf > nf else after
            else:
                pick = after
        else:
            pick = after or before
        if pick:
            nxt = None
            if not pick.group('b'):                  # a lone start: the next act's start ends this set
                nxt = next((t for t in toks if t.start() >= max(pick.end(), e) and not t.group('b')
                            and t.start() < max(pick.end(), e) + 300), None)
            c = to_set(pick, start, nxt)
            if c:
                lo, hi = min(pick.start(), s), max(pick.end(), e)
                c['snippet'] = ' '.join(text[max(0, lo - 40):hi + 40].split())
                cands.append(c)
        win = ft[max(0, s - WINDOW):e + WINDOW]
        for k in KW.finditer(win):
            kws.add('txt:' + ' '.join(win[k.start():k.start() + 60].split()))
    if are.search(fold(title or '')):
        for img in re.finditer(r'<img\b[^>]*>', html_text, re.I):
            attrs = ' '.join(v for _, v in re.findall(r'\b(alt|title|src)\s*=\s*"([^"]*)"', img.group(0), re.I))
            if KW.search(fold(attrs)):
                kws.add('img:' + attrs[:120])
    cands.sort(key=lambda c: (c.get('assumed_end', False),))
    return cands, kws, mentions


def strong(sep):
    return bool(re.search(r'[|\u00b7\u2022,/]', sep))


def to_set(m, start, nxt=None):
    h, mi = clock(m.group('a'))
    s = start.replace(hour=h, minute=mi, second=0, microsecond=0)
    if s < start - dt.timedelta(hours=2):
        s += dt.timedelta(days=1)
    if s > start + dt.timedelta(hours=14):
        return None
    c = {'start': s.isoformat()}
    end_tok = m.group('b') or (nxt.group('a') if nxt else None)
    if end_tok:
        h2, m2 = clock(end_tok)
        e = s.replace(hour=h2, minute=m2)
        if e <= s:
            e += dt.timedelta(days=1)
        if dt.timedelta(minutes=30) <= e - s <= dt.timedelta(hours=8 if m.group('b') else 4):
            c['end'] = e.isoformat()
            if not m.group('b'):
                c['end_from'] = 'next act'
    if 'end' not in c:
        if (s.hour, s.minute) == (start.hour, start.minute):
            return None                      # a lone time equal to the start = doors / the event listing
        c['assumed_end'] = True
    return c


# ---------------------------------------------------------------- fetching

def fetch(st, src, force=False):
    """-> ('ok', html) | ('same', None) | ('wait', None); raises Perm or watch's WatchError."""
    w = W()
    url = src['url']
    why = w.banned_reason(url)
    if why:
        raise Perm(why)
    host = w.host_of(url)
    hs = st['hosts'].setdefault(host, {})
    t = now()
    if not force and t - hs.get('last', 0) < HOST_GAP:
        return 'wait', None
    if not force and src.get('retry_at', 0) > t:
        return 'wait', None
    hs['last'] = t
    refusal = w.robots_check(st['hosts'], url)
    if refusal:
        raise Perm(refusal)
    r = w.http_get(url, etag=src.get('etag'), modified=src.get('modified'))
    if r.status == 304:
        return 'same', None
    if r.status in (401, 403, 429) and w.is_bot_wall(r):     # as watch: only an error page can be a wall
        raise Perm(f'{host} answers with a bot check (HTTP {r.status}); not bypassed')
    if r.status in (429, 503) or r.status >= 500:
        src['retry_at'] = t + (w.retry_after(r.headers) or 3 * HOUR)
        raise w.WatchError(f'HTTP {r.status}')
    if r.status != 200:
        raise w.WatchError(f'HTTP {r.status}')
    src['etag'] = r.headers.get('ETag') if r.headers else None
    src['modified'] = r.headers.get('Last-Modified') if r.headers else None
    return 'ok', r.text()


# ---------------------------------------------------------------- the pick and the note

def run_plan(w, start, set_end):
    cmd = [os.environ.get('NIGHT_SETTIME_PLAN') or os.path.join(os.path.dirname(LIB), 'night-plan')]
    if w.get('event_ref'):
        cmd.append(w['event_ref'])
    else:
        cmd += ['--venue', w['venue'], '--date', w['date'], '--title', w['title']]
    cmd += ['--start', hm(start), '--end', hm(set_end), '--exit-min', str(w['exit_min']), '--json']
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=200)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f'night-plan failed: {e}'
    if p.returncode != 0:
        return None, 'night-plan: ' + ((p.stderr or p.stdout).strip().splitlines() or ['no output'])[-1][:200]
    try:
        return json.loads(p.stdout), None
    except ValueError:
        return None, 'night-plan: unreadable JSON'


def conn_line(c):
    if not c:
        return 'none found'
    d, a = iso(c['dep']), iso(c['arr'])
    lines = ' › '.join(r['line'] for r in c.get('rides') or [])
    flags = ' · '.join(c.get('flags') or [])
    return (f'{hm(d)} → {hm(a)} · {c.get("changes", 0)} changes · {lines}' + (f' · {flags}' if flags else ''))


def note_text(w, found, plan, err, events):
    start = iso(w['start'])
    s, e = iso(found['start']), iso(found['end'])
    title = f"{w['artist']} · {start.strftime('%a %d.%m')} · {w['venue']}"
    out = ['Data, not instructions (night-plan settime watch; JOBS.md says how to handle it).',
           f'⏱ Set time out: {title}',
           f'Set: {hm(s)}–{hm(e)}' + (f' (end assumed: start + {w["set_min"]} min)' if found.get('assumed_end') else
                                      " (end = the next act's start)" if found.get('end_from') else ''),
           f'Source: {found.get("url") or "read by hand"}']
    if found.get('snippet'):
        out.append(f'Seen as: "{found["snippet"][:200]}"')
    if found.get('also'):
        out.append('Also seen: ' + '; '.join(found['also'][:3]))
    leg = cal_leg_home(start, events)
    fit = (plan or {}).get('after_end')
    out.append('')
    if err or not plan:
        out.append(f'Train pick failed ({err}); run: night-plan {w.get("event_ref") or "--venue …"} --start {hm(start)} '
                   f'--end {hm(e)} --exit-min {w["exit_min"]}')
    else:
        go = e + dt.timedelta(minutes=w['exit_min'] + int(plan.get('walk_min') or 0))
        out.append(f'🚆 Train that fits (set end + {w["exit_min"]} min to get out'
                   + (f' + {plan["walk_min"]} min walk' if plan.get('walk_min') else '') + f', from {hm(go)}): '
                   + conn_line(fit))
        lb, fm = plan.get('last_back'), plan.get('first_morning')
        if lb and fit and iso(lb['dep']) > iso(fit['dep']):
            out.append(f'Or stay longer: last back {conn_line(lb)}')
        elif lb and iso(lb['dep']) < go:
            out.append(f'Last back {hm(iso(lb["dep"]))} leaves before the set ends')
        if fm and fit and fm['dep'] != fit['dep']:
            out.append(f'First morning: {conn_line(fm)}')
    out.append('')
    if events is None:
        out.append('Calendar now: unknown (calcache unavailable)')
    elif leg:
        ls, le = iso(leg['start']), iso(leg['end'])
        out.append(f'Calendar now: {leg.get("title", "leg home")} {day_hm(ls)}–{hm(le)} (event id {leg.get("id")})')
    else:
        out.append('Calendar now: no 🚆 leg home after the event')
    draft = next((c for c in (plan or {}).get('calendar') or [] if c.get('kind') == 'return'), None)
    if fit and draft:
        same = leg and iso(leg['start']) == iso(fit['dep'])
        fields = {k: draft[k] for k in ('summary', 'startTime', 'endTime', 'location', 'description', 'timeZone')
                  if k in draft}
        if same:
            out.append('Draft calendar change: none, the leg in the calendar already is this train.')
        elif leg:
            delta = int((iso(fit['dep']) - iso(leg['start'])).total_seconds() // 60)
            out.append(f'Draft calendar change (not applied): update_event {leg.get("id")} → '
                       f'{hm(iso(fit["dep"]))}–{hm(iso(fit["arr"]))} ({"%+d" % delta} min vs now):')
            out.append(json.dumps({k: v for k, v in fields.items() if k != 'summary'}, ensure_ascii=False))
        else:
            out.append('Draft calendar change (not applied): create_event:')
            out.append(json.dumps(fields, ensure_ascii=False))
        if not same:
            out.append('After a change: calcache refresh (or train-watch sync) so train-watch follows the new leg.')
    return '\n'.join(out) + '\n'


def finish(st, w, found, dry=False):
    start = iso(w['start'])
    plan, err = run_plan(w, start, iso(found['end']))
    text = note_text(w, found, plan, err, calendar_events())
    if dry:
        return text, None
    f = write_note(f'{slug(w["artist"])}-settime', text)
    w['status'], w['found'] = 'found', dict(found, note=os.path.basename(f), at=now())
    return text, f


# ---------------------------------------------------------------- check

def interval(w, t):
    left = iso(w['start']).timestamp() - t
    return 1800 if left < 24 * HOUR else HOUR if left < 72 * HOUR else 3 * HOUR


def check_watch(st, wid, w, force=False, dry=False, verbose=False):
    t = now()
    if w['status'] not in ('watching',):
        return []
    if t > w['until']:
        w['status'] = 'expired'
        return [f'{wid}: expired']
    if not force and t < w.get('next_at', 0):
        return []
    start = iso(w['start'])
    msgs, best, waited = [], [], False
    for src in w['sources']:
        if src.get('state') == 'refused':
            continue
        try:
            kind, body = fetch(st, src, force=force)
        except Perm as e:
            src.update(state='refused', error=str(e)[:200])
            msgs.append(f'{wid}: {src["url"]} refused: {e}')
            continue
        except Exception as e:                       # watch's WatchError and anything network-shaped
            src['fails'] = src.get('fails', 0) + 1
            src.update(state='failing', error=str(e)[:200], checked=t)
            msgs.append(f'{wid}: {src["url"]} failed: {e}')
            continue
        if kind == 'wait':
            waited = True
            continue
        src.update(state='ok', fails=0, error=None, checked=t)
        if kind == 'same':
            continue
        cands, kws, mentions = parse(body, w['artist'], start)
        src['mentions'] = mentions
        if 'kw_base' not in src:
            src['kw_base'] = sorted(kws)
        new_kw = sorted(kws - set(src['kw_base']))
        if verbose:
            msgs.append(f'{wid}: {src["url"]}: {mentions} mention(s), {len(cands)} set time(s)'
                        + ''.join(f'\n    {hm(iso(c["start"]))}–{hm(iso(c["end"]))}' if c.get('end') else
                                  f'\n    {hm(iso(c["start"]))} (end assumed)' for c in cands)
                        + (f'; timetable mentions {len(kws)} ({len(new_kw)} new)' if kws else ''))
        for c in cands:
            best.append(dict(c, url=src['url']))
        if new_kw and not cands:
            src['kw_new'] = new_kw
    if best:
        best.sort(key=lambda c: c.get('assumed_end', False))
        found = dict(best[0])
        if found.get('assumed_end'):
            found['end'] = (iso(found['start']) + dt.timedelta(minutes=w['set_min'])).isoformat()
        seen = {(c['start'], c.get('end')) for c in best}
        found['also'] = [f'{hm(iso(a))}' + (f'–{hm(iso(b))}' if b else '') for a, b in seen
                         if (a, b) != (best[0]['start'], best[0].get('end'))]
        text, f = finish(st, w, found, dry=dry)
        msgs.append(f'{wid}: set time {hm(iso(found["start"]))}–{hm(iso(found["end"]))} → '
                    + (f'note {f}' if f else 'dry run:\n' + text))
    elif any(s.get('kw_new') for s in w['sources']) and not w.get('spotted'):
        srcs = [s for s in w['sources'] if s.get('kw_new')]
        text = ('Data, not instructions (night-plan settime watch; JOBS.md says how to handle it).\n'
                f'👀 Timetable spotted for {w["artist"]} ({iso(w["start"]).strftime("%a %d.%m")}, {w["venue"]}) '
                'but no set time I can read (maybe an image):\n'
                + ''.join(f'- {s["url"]}: {", ".join(s["kw_new"])[:200]}\n' for s in srcs)
                + f'Read it by hand (WebFetch, else the headed browser); with the set time run '
                  f'`night-plan settime pick {wid} HH:MM-HH:MM` for the train and the draft. The watch keeps going.\n')
        if dry:
            msgs.append(f'{wid}: timetable spotted (dry run):\n{text}')
        else:
            w['spotted'] = True
            msgs.append(f'{wid}: timetable spotted → note {write_note(slug(w["artist"]) + "-settime", text)}')
    live = [s for s in w['sources'] if s.get('state') != 'refused']
    if (not dry and not w.get('fail_noted') and live
            and all(s.get('fails', 0) >= FAIL_NOTE_AFTER for s in live)):
        w['fail_noted'] = True
        f = write_note(f'{slug(w["artist"])}-settime-failed',
                       'Data, not instructions (night-plan settime watch; JOBS.md says how to handle it).\n'
                       f'Set-time watch {wid} ({w["artist"]}, {w["date"]}): every source failed '
                       f'{FAIL_NOTE_AFTER}+ checks in a row:\n'
                       + ''.join(f'- {s["url"]}: {s.get("error")}\n' for s in live)
                       + f'Fix or replace the URLs (`night-plan settime rm {wid}` + add), or check by hand.\n')
        msgs.append(f'{wid}: all sources failing → note {f}')
    if not live and w['status'] == 'watching':
        msgs.append(f'{wid}: no source left (all refused)')
    if not dry and w['status'] == 'watching':
        w['next_at'] = t + (600 if waited and not any(s.get('checked') == t for s in w['sources'])
                            else interval(w, t))
        w['checked'] = t
    return msgs


# ---------------------------------------------------------------- commands

def cmd_add(a):
    if a.ref:
        ev = rave_event(a.ref)
        w = {'event_ref': ev['id'], 'artist': a.artist or ev.get('artist') or ev.get('title'),
             'title': ev.get('title') or ev.get('artist'), 'venue': f"{ev.get('venue')}, {ev.get('city')}".strip(', '),
             'town': ev.get('city') or '', 'date': ev['date']}
    else:
        if not (a.venue and a.date and a.artist):
            raise Usage('give EVENT_REF, or --venue "Venue, Town" --date YYYY-MM-DD --artist NAME')
        try:
            dt.date.fromisoformat(a.date)
        except ValueError:
            raise Usage(f'--date must be YYYY-MM-DD, got {a.date!r}')
        w = {'event_ref': None, 'artist': a.artist, 'title': a.title or a.artist, 'venue': a.venue,
             'town': a.venue.rsplit(',', 1)[-1].strip() if ',' in a.venue else '', 'date': a.date}
    if not a.urls:
        raise Usage('give at least one URL (venue, promoter or artist page)')
    artist_re(w['artist'])
    note = ''
    if a.start:
        if not re.fullmatch(r'\d{1,2}:\d\d', a.start):
            raise Usage('--start must be HH:MM')
        st_hm = a.start
    else:
        ce = cal_event(w, calendar_events())
        if ce:
            st_hm, note = iso(ce['start']).strftime('%H:%M'), ' (from the calendar)'
        else:
            st_hm, note = '22:00', ' (assumed; give --start)'
    h, m = map(int, st_hm.split(':'))
    start = dt.datetime.combine(dt.date.fromisoformat(w['date']), dt.time(h, m), TZ)
    until = (dt.datetime.strptime(a.until, '%Y-%m-%d %H:%M').replace(tzinfo=TZ) if a.until
             else start + dt.timedelta(hours=4))
    srcs = []
    for u in a.urls:
        if not re.match(r'https?://', u):
            raise Usage(f'not an http(s) URL: {u}')
        why = W().banned_reason(u)
        if why:
            raise Usage(f'{u}: refused: {why}')
        srcs.append({'url': u})
    w.update(start=start.isoformat(), set_min=a.set_min, exit_min=a.exit_min, until=until.timestamp(),
             sources=srcs, status='watching', created=now(), next_at=0)
    wid = hashlib.sha1(f"{w['event_ref'] or w['title']}|{w['date']}|{w['artist']}".encode()).hexdigest()[:6]
    with Locked():
        st = load()
        if wid in st['watches'] and st['watches'][wid]['status'] == 'watching':
            st['watches'][wid]['sources'] = srcs
            print(f'settime {wid}: sources replaced')
        else:
            st['watches'][wid] = w
        msgs = check_watch(st, wid, st['watches'][wid], force=True, verbose=True)
        save(st)
    w = st['watches'][wid]
    print(f'settime {wid}: {w["artist"]} · {w["date"]} · start {st_hm}{note} · until {day_hm(local(w["until"]))} · '
          f'{len(srcs)} source(s)')
    for m in msgs:
        print('  ' + m)
    for s in w['sources']:
        if s.get('kw_base'):
            print(f'  baseline: {len(s["kw_base"])} timetable mention(s) already on {s["url"]} (not reported)')
    return 0


def cmd_list(a):
    st = load()
    if a.json:
        print(json.dumps(st['watches'], indent=1, ensure_ascii=False))
        return 0
    if not st['watches']:
        print('no set-time watches')
    for wid, w in st['watches'].items():
        extra = ''
        if w.get('found'):
            f = w['found']
            extra = f' · set {hm(iso(f["start"]))}–{hm(iso(f["end"]))} → {f.get("note")}'
        nxt = f' · next {day_hm(local(w["next_at"]))}' if w['status'] == 'watching' and w.get('next_at') else ''
        print(f'{wid}  {w["artist"]} · {w["date"]} {hm(iso(w["start"]))} · {w["venue"]} · {w["status"]}{nxt}{extra}')
        for s in w['sources']:
            when = day_hm(local(s['checked'])) if s.get('checked') else 'not yet'
            err = f' ({s.get("error")})' if s.get('error') else ''
            print(f'    {s.get("state", "new"):8} {when:16} {s["url"]}{err}')
    return 0


def cmd_rm(a):
    with Locked():
        st = load()
        if a.id not in st['watches']:
            raise Usage(f'no watch {a.id}')
        del st['watches'][a.id]
        save(st)
    print(f'settime {a.id} removed')
    return 0


def cmd_check(a):
    out = []
    with Locked():
        st = load()
        ids = [a.id] if a.id else list(st['watches'])
        if a.id and a.id not in st['watches']:
            raise Usage(f'no watch {a.id}')
        for wid in ids:
            out += check_watch(st, wid, st['watches'][wid], force=a.force or a.dry_run or bool(a.id),
                               dry=a.dry_run, verbose=not a.cron)
        if not a.dry_run:
            save(st)
    for m in out:
        if not a.cron or 'note' in m or 'refused' in m:
            print(m)
    return 0


def cmd_pick(a):
    st = load()
    w = st['watches'].get(a.id)
    if not w:
        raise Usage(f'no watch {a.id}')
    m = TOK.fullmatch(fold(a.time.strip()))
    if not m:
        raise Usage('time must be HH:MM or HH:MM-HH:MM')
    c = to_set(m, iso(w['start']))
    if not c:
        raise Usage('that time does not fit the event night (or equals the event start without an end)')
    if c.get('assumed_end'):
        c['end'] = (iso(c['start']) + dt.timedelta(minutes=w['set_min'])).isoformat()
    text, _ = finish(st, w, dict(c, url=None), dry=True)
    print(text, end='')
    return 0


def main(argv):
    ap = argparse.ArgumentParser(prog='night-plan settime', description=__doc__.split('\n')[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog='\n'.join(__doc__.split('\n')[1:]))
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('add')
    p.add_argument('ref', nargs='?', help='rave-radar event id (or unique prefix)')
    p.add_argument('urls', nargs='*')
    p.add_argument('--venue')
    p.add_argument('--date')
    p.add_argument('--title')
    p.add_argument('--artist')
    p.add_argument('--start')
    p.add_argument('--set-min', type=int, default=120)
    p.add_argument('--exit-min', type=int, default=20)
    p.add_argument('--until')
    p = sub.add_parser('list')
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('rm')
    p.add_argument('id')
    p = sub.add_parser('check')
    p.add_argument('id', nargs='?')
    p.add_argument('--cron', action='store_true')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--force', action='store_true')
    p = sub.add_parser('pick')
    p.add_argument('id')
    p.add_argument('time')
    a = ap.parse_args(argv)
    if a.cmd == 'add' and a.ref and re.match(r'https?://', a.ref):
        a.urls, a.ref = [a.ref] + a.urls, None
    try:
        return {'add': cmd_add, 'list': cmd_list, 'rm': cmd_rm, 'check': cmd_check, 'pick': cmd_pick}[a.cmd](a)
    except Usage as e:
        print(f'night-plan settime: {e}', file=sys.stderr)
        return 2
    except (OSError, ValueError) as e:
        print(f'night-plan settime: {e}', file=sys.stderr)
        return 1
