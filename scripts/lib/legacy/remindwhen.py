"""remindwhen: a reminder's natural "when" -> owner wall time or a place trigger (token-free, no network).

    parse(phrase, now, events=None) -> When(kind='at'|'on', at=datetime|None, on=str|None, why=str, anchor=dict|None)

Used by `remind when|add|snooze|move`. English and German. ValueError (with a hint) when unusable.

  exact      "2026-10-23 18:00", "2026-10-23", "23.10.", "23.10.2026 18:30"
  relative   "+1h", "+30m", "in 2 hours", "in 20 min", "in half an hour", "in 3 days", "in 2 Stunden", "in einer Stunde"
  day+time   "tomorrow evening", "friday 18:00", "next monday at 7", "morgen früh", "übermorgen um 14 Uhr",
             "tonight", "this weekend", "next week", "6pm", "18h", "um 9"
             day parts: morning/früh 09:00, noon/mittag 12:00, after lunch 13:30, afternoon/nachmittag 15:00,
             evening/tonight/abend 19:00, night/nacht 21:00, bedtime/vor dem schlafen 22:00
             a weekday means the next one (never today); no time = 09:00; a bare 1-11 ("at 7") = the next of
             07:00/19:00 that is still ahead
  calendar   "after the concert", "before my flight", "2h before the train", "30 min nach dem Termin", "nach der
             Hochzeit": the next matching event of the calendar cache (calcache). before = start - 1 h (flight 3 h,
             train 15 min) unless an offset is given; after = its end; "at/during X" = its start.
             Match: words of the title first, then a kind (flight/train/drive legs; concert/party/rave/festival/
             show = a timed event marked free; appointment/termin/event = the next timed non-travel event).
  place      "when I get home", "wenn ich heimkomme" -> on=home; "when I leave (home)" -> leave;
             "when I get to work", "at the office" -> zone:Work; "when I leave work", "after work",
             "nach der Arbeit" -> leave:zone:Work; "when I arrive at Bern" -> zone:Bern
"""
import json, os, re, unicodedata
from datetime import datetime, timedelta, time as dtime

FMT = '%Y-%m-%d %H:%M'
PARTS = [  # (regex, hour, minute, label) — longest phrases first
    (r'after lunch|nach dem mittag(?:essen)?', 13, 30, 'after lunch'),
    (r'before bed(?:time)?|bedtime|vor dem (?:schlafen(?:gehen)?|ins bett gehen)|vorm schlafen', 22, 0, 'bedtime'),
    (r'(?:in the |this |am |heute )?(?:morning|früh|frueh|morgens|vormittags?|am morgen)', 9, 0, 'morning'),
    (r'(?:at |am )?(?:noon|midday|lunch(?:time)?|mittags?|zu mittag)', 12, 0, 'noon'),
    (r'(?:in the |this |am )?(?:afternoon|nachmittags?)', 15, 0, 'afternoon'),
    (r'(?:in the |this |am |heute )?(?:evening|tonight|abends?)', 19, 0, 'evening'),
    (r'(?:at |in der |heute )?(?:night|nachts?|late)', 21, 0, 'night'),
]
DAYS = {'mon': 0, 'monday': 0, 'mo': 0, 'montag': 0, 'tue': 1, 'tues': 1, 'tuesday': 1, 'di': 1, 'dienstag': 1,
        'wed': 2, 'wednesday': 2, 'mi': 2, 'mittwoch': 2, 'thu': 3, 'thur': 3, 'thurs': 3, 'thursday': 3, 'do': 3,
        'donnerstag': 3, 'fri': 4, 'friday': 4, 'fr': 4, 'freitag': 4, 'sat': 5, 'saturday': 5, 'sa': 5,
        'samstag': 5, 'sun': 6, 'sunday': 6, 'so': 6, 'sonntag': 6}
NUM = {'a': 1, 'an': 1, 'one': 1, 'ein': 1, 'eine': 1, 'einer': 1, 'einen': 1, 'two': 2, 'zwei': 2, 'three': 3,
       'drei': 3, 'four': 4, 'vier': 4, 'five': 5, 'fünf': 5, 'ten': 10, 'zehn': 10, 'twenty': 20, 'zwanzig': 20,
       'fifteen': 15, 'fünfzehn': 15, 'thirty': 30, 'dreissig': 30, 'dreißig': 30, 'few': 3, 'paar': 2}
UNIT = [(r'm|min|mins|minutes?|minuten?', 'm'), (r'h|hr|hrs|hours?|std|stunden?', 'h'),
        (r'd|days?|tagen?|tage', 'd'), (r'w|weeks?|wochen?', 'w')]
WORK = ('work', 'the office', 'office', 'arbeit', 'der arbeit', 'die arbeit', 'büro', 'buero', 'dem büro',
        'das büro', 'im büro', 'geschäft', 'job')
HOME = ('home', 'at home', 'zuhause', 'zu hause', 'daheim', 'heim', 'nach hause')
# calendar kinds: keyword -> (kind, default minutes before)
KINDS = {'flight': 'flight', 'flug': 'flight', 'plane': 'flight', 'flieger': 'flight', 'abflug': 'flight',
         'train': 'train', 'zug': 'train', 'bahn': 'train', 'drive': 'drive', 'fahrt': 'drive', 'trip': 'drive',
         'concert': 'free', 'konzert': 'free', 'party': 'free', 'rave': 'free', 'festival': 'free', 'show': 'free',
         'gig': 'free', 'club': 'free', 'event': 'any', 'appointment': 'any', 'termin': 'any', 'meeting': 'any',
         'next event': 'any', 'nächsten termin': 'any', 'next appointment': 'any'}
BEFORE_MIN = {'flight': 180, 'train': 15}
SYN = [('wedding', 'hochzeit', 'hochziit'), ('concert', 'konzert'), ('birthday', 'geburtstag', 'geburi', 'bday'), ('dinner', 'abendessen', 'znacht'),
       ('lunch', 'mittagessen', 'zmittag'), ('dentist', 'zahnarzt'), ('doctor', 'arzt', 'doc'), ('haircut', 'coiffeur', 'friseur'),
       ('gym', 'training', 'fitness'), ('dance', 'tanz', 'tanzkurs'), ('meeting', 'sitzung', 'besprechung'),
       ('party', 'fest', 'feier'), ('flight', 'flug'), ('train', 'zug'), ('office', 'büro', 'buero')]
FILLER = r'(?:the|my|a|an|dem|der|den|das|die|meinem|meiner|meinen|meine|mein|einem|einer|nächsten|next)\s+'


class When:
    def __init__(self, kind, at=None, on=None, why='', anchor=None):
        self.kind, self.at, self.on, self.why, self.anchor = kind, at, on, why, anchor

    def as_dict(self):
        d = {'kind': self.kind, 'why': self.why}
        if self.at:
            d['at'] = self.at.strftime(FMT)
        if self.on:
            d['on'] = self.on
        if self.anchor:
            d['anchor'] = self.anchor
        return d


def _norm(s):
    s = unicodedata.normalize('NFC', s).strip().lower()
    s = re.sub(r'[,!?]+', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return re.sub(r'(?<=\D)\.$', '', s).strip()      # a sentence dot, not the one of "23.10."



def _zone_name(raw):
    raw = raw.strip()
    if raw.lower() in WORK:
        return 'Work'
    raw = re.sub(r'^' + FILLER, '', raw, flags=re.I)
    return raw[:1].upper() + raw[1:] if raw else raw


def place(s):
    """A place trigger for the phrase (normalized), else None."""
    t = re.sub(r'^(?:remind me |erinnere mich )', '', s)
    t = re.sub(r"\bi'm\b", 'i am', t)
    if re.fullmatch(r"(?:when|once|as soon as) i(?: am)? (?:get|come|arrive|am back|back)(?: back)?(?: home| at home| to home)?|"
                    r"(?:when|once) i(?: am)? (?:home|at home|back home)|at home|(?:when|once) home|"
                    r"wenn ich (?:wieder )?(?:nach hause|heim|zu hause|zuhause|daheim) ?(?:komme|bin)|wenn ich heimkomme|"
                    r"wenn ich zurück bin|zu hause|zuhause|daheim|on arrival home", t):
        return 'home'
    if re.fullmatch(r"(?:when|once|as soon as) i (?:leave|go out|head out)(?: the house| home| the flat| the apartment)?|"
                    r"when leaving(?: home| the house)?|wenn ich (?:das haus|die wohnung)? ?verlasse|"
                    r"wenn ich (?:von zu hause |von daheim )?(?:losgehe|weggehe|rausgehe|los gehe)|beim (?:rausgehen|losgehen)", t):
        return 'leave'
    m = re.fullmatch(r"(?:when|once|as soon as) i (?:leave|finish(?: at)?|am done at|get off|walk out of) (.+)|"
                     r"wenn ich (?:die |das |den )?(.+?) verlasse|after (work|the office)|nach (?:der )?(arbeit|dem büro|feierabend)|"
                     r"(?:at|bei) feierabend", t)
    if m:
        name = next((g for g in m.groups() if g), 'work')
        name = 'work' if name in ('feierabend',) else name
        if name.strip() in HOME or name.strip() in ('the house', 'the flat', 'die wohnung', 'das haus'):
            return 'leave'
        return 'leave:zone:' + _zone_name(name)
    m = re.fullmatch(r"(?:when|once|as soon as) i(?: am)? (?:get|arrive|am|reach|come) (?:to|at|in|into)? ?(.+)|"
                     r"(?:at|in) (work|the office|office)|"
                     r"wenn ich (?:in|im|bei|auf|an) (?:der |dem |die |das )?(.+?) (?:bin|ankomme)|"
                     r"wenn ich (?:zur|zum|ins|nach) (.+?) (?:komme|gehe)|(?:bei der|auf der|in der|im) (arbeit|büro)", t)
    if m:
        name = next((g for g in m.groups() if g), '')
        name = re.sub(r'^(?:to|at|in) ', '', name)
        if not name or re.search(r'\d', name):
            return None
        if name in HOME or name in ('home',):
            return 'home'
        return 'zone:' + _zone_name(name)
    return None


def _clock(t):
    """'18:00' / '8.30' / '18h' / '18 uhr' / '18uhr30' / '6pm' / '6:30 pm' / '7' -> (h, m, sure) or None.
    sure = no am/pm doubt (24 h forms, am/pm, or a bare hour >= 12)."""
    t = re.sub(r'\s+', '', t)
    m = re.fullmatch(r'(\d{1,2})(?:[:.](\d{2}))?(am|pm)', t)
    if m:
        h, mi, sure = int(m[1]) % 12 + (12 if m[3] == 'pm' else 0), int(m[2] or 0), True
    else:
        m = re.fullmatch(r'(\d{1,2})[:.](\d{2})(?:uhr|h)?', t) or re.fullmatch(r'(\d{1,2})(?:uhr|h)(\d{2})?', t)
        if m:
            h, mi, sure = int(m[1]), int(m[2] or 0), True
        else:
            m = re.fullmatch(r'(\d{1,2})', t)
            if not m:
                return None
            h, mi = int(m[1]), 0
            sure = h >= 12 or h == 0 or t.startswith('0')
    if h > 23 or mi > 59:
        return None
    return h, mi, sure


def _relative(s, now):
    s2 = re.sub(r'^(?:in|nach|after)\s+', '', s)
    s2 = re.sub(r'^\+\s*', '', s2)
    if re.fullmatch(r'(?:half an? hour|einer? halben? stunde|30 ?min)', s2):
        return now + timedelta(minutes=30), 'in 30 min'
    m = re.fullmatch(r'(\d{1,4}|' + '|'.join(map(re.escape, NUM)) + r')\s*(' + '|'.join(u for u, _ in UNIT) + r')'
                     r'(?:\s+(?:and|und)\s+(?:a\s+)?half|\s*\d{1,2}\s*(?:m|min))?', s2)
    if not m or not s.startswith(('in ', '+', 'nach ', 'after ')):    # bare "18h" is a clock time
        return None
    n = int(m[1]) if m[1].isdigit() else NUM[m[1]]
    unit = next(u for pat, u in UNIT if re.fullmatch(pat, m[2]))
    if n == 0:
        raise ValueError('pick at least one minute from now')
    delta = {'m': timedelta(minutes=n), 'h': timedelta(hours=n), 'd': timedelta(days=n), 'w': timedelta(weeks=n)}[unit]
    extra = re.search(r'(\d{1,2})\s*(?:m|min)$', s2[m.start(2):]) if unit == 'h' else None
    if extra and m.end() > m.end(2):
        delta += timedelta(minutes=int(extra[1]))
    if 'half' in s2[m.end(2):]:
        delta += {'h': timedelta(minutes=30), 'd': timedelta(hours=12)}.get(unit, timedelta(0))
    at = now + delta
    if unit in ('d', 'w') and not re.search(r'\d{1,2}:\d{2}', s):
        at = at.replace(hour=9, minute=0) if at.hour < 6 or at.hour > 21 else at
    return at.replace(second=0, microsecond=0), f'in {m[1]} {m[2]}'


def load_events(path=None):
    p = path or os.path.join(os.environ.get('CALCACHE_DIR') or os.path.expanduser('~/.local/state/calcache'), 'events.json')
    try:
        with open(p) as f:
            d = json.load(f)
        return [e for e in d.get('events', []) if isinstance(e, dict)]
    except (OSError, ValueError, AttributeError):
        return None


def _ev_time(s, tz_naive=True):
    t = datetime.fromisoformat(s)
    return t.replace(tzinfo=None) if tz_naive else t


def _fold(s):
    return ''.join(c for c in unicodedata.normalize('NFD', s.lower()) if unicodedata.category(c) != 'Mn')


def _event_kind(e):
    tr = (e.get('flags') or {}).get('travel')
    if tr:
        return tr
    return 'free' if e.get('free') else 'busy'


def match_event(key, events, now, tz=None):
    """The next timed event (start or end still ahead) matching KEY: title words first, then a kind."""
    def local(s):
        t = datetime.fromisoformat(s)
        if t.tzinfo and tz:
            t = t.astimezone(tz)
        return t.replace(tzinfo=None)
    timed = []
    for e in events or []:
        if e.get('all_day') or 'T' not in str(e.get('start', '')):
            continue
        try:
            st, en = local(e['start']), local(e.get('end') or e['start'])
        except (ValueError, TypeError):
            continue
        if en > now:
            timed.append((st, en, e))
    timed.sort(key=lambda x: x[0])
    k = _fold(re.sub(r'^' + FILLER, '', key).strip())
    k = re.sub(r'^' + FILLER, '', k).strip()
    words = [w for w in re.findall(r'\w+', k) if len(w) >= 3]
    alts = [[_fold(x) for x in next((g for g in SYN if w in map(_fold, g)), (w,))] for w in words]
    if words:
        for st, en, e in timed:
            title = _fold(e.get('title') or '')
            if all(any(re.search(r'\b' + re.escape(x), title) for x in alt) for alt in alts):
                return st, en, e, 'title'
    kind = KINDS.get(k) or KINDS.get(k.rstrip('s')) or KINDS.get(words[0] if words else '')
    if not kind:
        return None
    for st, en, e in timed:
        ek = _event_kind(e)
        if kind == 'any' and ek in ('free', 'busy') or ek == kind:
            return st, en, e, kind
    return None


def _anchor(s, now, events, tz):
    m = re.fullmatch(r'(?:(\d{1,3}|half an?|an?|one|eine?r?) ?(m|min|mins|minutes?|minuten|h|hrs?|hours?|std|stunden?) )?'
                     r'(before|after|vor|nach|at|during|bei|beim|während) (.+)', s)
    if not m:
        return None
    rel, key = m[3], m[4]
    if any(re.fullmatch(p, s) for p, _, _, _ in PARTS[:2]):
        return None                                  # "after lunch", "before bed": day parts
    if rel == 'at' and not re.match(FILLER, key + ' '):
        return None                                  # "at 7" / "when …" are not calendar anchors
    if _clock(key) or re.fullmatch(r'\d{1,2}', key):
        return None
    if events is None:
        raise ValueError('no calendar cache (calcache) to look up "%s": give a time instead' % key)
    hit = match_event(key, events, now, tz)
    if not hit:
        raise ValueError('no upcoming calendar event matches "%s" (calcache, next 21 days): give a time' % key)
    st, en, e, how = hit
    off = None
    if m[1]:
        n = 30 if m[1].startswith('half') else (1 if m[1] in ('a', 'an', 'one', 'ein', 'eine', 'einer') else int(m[1]))
        off = timedelta(hours=n) if m[2][0] in 'hs' else timedelta(minutes=n)
    kind = _event_kind(e)
    if rel in ('before', 'vor'):
        at = st - (off if off is not None else timedelta(minutes=BEFORE_MIN.get(kind, 60)))
        rel_txt = 'before'
    elif rel in ('after', 'nach'):
        at = en + (off or timedelta(0))
        rel_txt = 'after'
    else:
        at = st
        rel_txt = 'at'
    at = at.replace(second=0, microsecond=0)
    if at <= now:
        raise ValueError('%s %s is already past (%s)' % (rel_txt, key, at.strftime('%a %d.%m %H:%M')))
    title = (e.get('title') or '')[:40]
    anchor = {'event': title, 'start': st.strftime(FMT), 'end': en.strftime(FMT), 'rel': rel_txt, 'match': how}
    gap = (st - at) if rel_txt == 'before' else (at - en) if rel_txt == 'after' else timedelta(0)
    mins = int(gap.total_seconds() // 60)
    lead = ('%d h %02d min ' % divmod(mins, 60) if mins >= 60 else '%d min ' % mins) if mins else ''
    why = '%s%s %s (%s %s–%s)' % (lead, rel_txt, title, st.strftime('%a %d.%m'), st.strftime('%H:%M'), en.strftime('%H:%M'))
    return at, why, anchor


def _day(s, now):
    """Strip a day phrase from s -> (date|None, rest, label)."""
    d = now.date()
    for pat, add, lab in ((r'(?:the )?day after tomorrow|übermorgen', 2, 'day after tomorrow'),
                          (r'tomorrow|tmrw|(?<!am )morgen', 1, 'tomorrow'),
                          (r'today|heute(?! abend)', 0, 'today'), (r'tonight|heute abend', 0, 'tonight')):
        m = re.search(r'(?:^|\s)(' + pat + r')(?:\s|$)', s)
        if m:
            rest = (s[:m.start(1)] + ' ' + s[m.end(1):]).strip()
            if lab == 'tonight':
                rest = (rest + ' evening').strip()
            return d + timedelta(days=add), rest, lab
    m = re.search(r'(?:^|\s)(?:(?:this|next|on|am|nächsten?|kommenden?|diesen?) )?(?:the )?(weekend|wochenende)(?:\s|$)', s)
    if m:
        nxt = 'next' in m[0] or 'nächst' in m[0]
        sat = d + timedelta(days=(5 - d.weekday()) % 7)
        if d.weekday() == 6 or nxt and d.weekday() < 5:
            sat += timedelta(days=7) if nxt and d.weekday() < 5 else timedelta(days=6)
        rest = (s[:m.start()] + ' ' + s[m.end():]).strip()
        return sat, rest or '10:00', 'weekend'
    m = re.search(r'(?:^|\s)(?:next|nächste) (week|woche)(?:\s|$)', s)
    if m:
        mon = d + timedelta(days=7 - d.weekday())
        return mon, (s[:m.start()] + ' ' + s[m.end():]).strip(), 'next week'
    m = re.search(r'(?:^|\s)(?:(next|this|on|am|nächsten|kommenden|diesen) )?(' + '|'.join(sorted(DAYS, key=len, reverse=True)) + r')\.?(?:\s|$)', s)
    if m:
        wd = DAYS[m[2]]
        ahead = (wd - d.weekday()) % 7 or 7          # a weekday = the next one, never today
        rest = (s[:m.start()] + ' ' + s[m.end():]).strip()
        return d + timedelta(days=ahead), rest, ('next ' if m[1] in ('next', 'nächsten', 'kommenden') else '') + m[2]
    m = re.search(r'(?:^|\s)(?:on |am )?(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})?(?=\s|$)', s)
    if m:
        y = int(m[3]) + (2000 if m[3] and len(m[3]) == 2 else 0) if m[3] else now.year
        try:
            dd = datetime(y, int(m[2]), int(m[1])).date()
        except ValueError:
            raise ValueError('no such date: %s.%s.' % (m[1], m[2]))
        if not m[3] and dd < d:
            dd = dd.replace(year=y + 1)
        return dd, (s[:m.start()] + ' ' + s[m.end():]).strip(), dd.strftime('%d.%m.')
    return None, s, ''


def _time(rest):
    """Day-part words and clock times in REST -> (h, m, sure, label) or None; ValueError if words are left over."""
    rest = re.sub(r'(\d)\s+(am|pm)\b', r'\1\2', rest)
    rest = re.sub(r"(?:^|\s)(?:at|um|gegen|by|on|am|o'clock|oclock)(?=\s|$)", ' ', rest)
    rest = re.sub(r'\s+', ' ', rest).strip()
    part = None
    for pat, h, mi, lab in PARTS:
        m = re.search(r'(?:^|\s)(?:' + pat + r')(?:\s|$)', rest)
        if m:
            part = (h, mi, lab)
            rest = (rest[:m.start()] + ' ' + rest[m.end():]).strip()
            break
    clock = None
    if rest:
        c = _clock(rest)
        if c is None:
            raise ValueError('could not read "%s"' % rest)
        clock = c
    if clock:
        h, mi, sure = clock
        if part and not sure and h < 12 and part[2] in ('afternoon', 'evening', 'night', 'bedtime'):
            h, sure = h + 12, True
        if part and not sure and part[2] == 'morning':
            sure = True
        return h, mi, sure, ''
    if part:
        return part[0], part[1], True, '%s = %02d:%02d' % (part[2], part[0], part[1])
    return None


def parse(phrase, now, events=None, tz=None):
    """See the module doc. NOW = naive owner wall time; events = calcache events (None = not loaded)."""
    raw = phrase or ''
    s = _norm(raw)
    if not s:
        raise ValueError('empty time')
    for f in ('%Y-%m-%d %H:%M', '%Y-%m-%dt%H:%M'):
        try:
            t = datetime.strptime(s, f)
            return When('at', at=t, why=t.strftime('%a %d.%m %H:%M'))
        except ValueError:
            pass
    try:
        t = datetime.strptime(s, '%Y-%m-%d').replace(hour=9)
        return When('at', at=t, why='no time given = 09:00')
    except ValueError:
        pass
    p = place(s)
    if p:
        return When('on', on=p, why=on_words(p))
    r = _relative(s, now)
    if r:
        return When('at', at=r[0], why=r[1])
    a = _anchor(re.sub(r'^(?:right |just |kurz |gleich |direkt )', '', s), now, events, tz)
    if a:
        return When('at', at=a[0], why=a[1], anchor=a[2])
    day, rest, dlab = _day(s, now)
    tm = _time(rest)
    if day is None and tm is None:
        raise ValueError('could not read "%s"' % raw)
    notes = [x for x in (dlab, tm[3] if tm else '') if x]
    if tm is None:
        h, mi, sure = 9, 0, True
        notes.append('no time given = 09:00')
    else:
        h, mi, sure = tm[:3]
    if day is None:
        t = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if not sure and h < 12:
            cands = [t, t + timedelta(hours=12), t + timedelta(days=1)]
            t = next(c for c in cands if c > now)
        elif t <= now:
            t += timedelta(days=1)
    else:
        t = datetime.combine(day, dtime(h, mi))
        if not sure and h < 12 and t <= now:
            t += timedelta(hours=12)
    if t <= now:
        raise ValueError('%s is already past' % t.strftime('%a %d.%m %H:%M'))
    return When('at', at=t, why=', '.join(notes) or t.strftime('%a %d.%m %H:%M'))


def on_words(on):
    if on == 'home':
        return 'when you get home'
    if on == 'leave':
        return 'when you leave home'
    if on.startswith('leave:zone:'):
        return 'when you leave %s' % on[11:]
    return 'when you get to %s' % on[5:]
