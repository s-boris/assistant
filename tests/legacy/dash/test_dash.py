#!/usr/bin/env python3
from zoneinfo import ZoneInfo
from datetime import datetime
"""Tests for ~/bin/dash-data, the data-free page template ~/opt/dash/index.html (repo: web/dash/index.html) and the site
dash-data generates from it in ~/.local/state/dash/www (index.html with the data inlined, data.json, dashboard.html).
Run: ~/opt/dash-tests/run.sh

1. clean(): masks addresses, e-mails, phones, IBAN/AHV, IPs, long numbers, token-like strings.
2. Fixture transcripts (temp dir): dedupe, cost maths, kinds, subagent meta, turn classes, cache reuse,
   and planted private text (address, token, e-mail) never reaches the output.
3. Real run: schema sanity, totals add up, data map references resolve.
4. Privacy sweep of the real data.json, template, generated index.html, dashboard.html: secretscan clean, no e-mails, phones,
   IPs, IBANs, card numbers, bot tokens, the owner's chat ids or any street from the memory notes.
5. Page structure: title, markers, no external requests, artifact fragment has no doctype/html/head/body.
   Template (also in a checkout/CI): data block {}, profile values only as {{key}} tokens dash-data may fill;
   the split on fixtures: template only read, www/ gets the page + data, {{network.server_name}} from the profile.
6. Headless Chrome render (DOM dump) of every tab (9 since the 2026-10-30 split of System into Health and CI:
   Health, CI, Agents, Today, Usage, Activity, Codebase, Privacy, Architecture; old hashes land on their new tab),
   light + dark phone screenshots to ~/reports/dash/, the work log's Today and Last night views from page copies
   (afternoon / morning data), and the "every metric once" rules: one line count, one agents-today number, one
   snapshot count, server health in Health. The split (fixture copies): the Health hero counts CI problems and opens
   the CI tab, the CI tab's cards and speed tiles, hub services nested under their containers, the Privacy highlight's
   short points, Agents' ended list open, all 9 tabs on one phone row pair. 11. Self-refresh: data.json every 60 s
   from a local server under virtual time, redrawn in place (an opened fold stays open), the stale marker; a new page
   version in data.json reloads the open tab once, back on its tab, fold and scroll.
9. Done metrics (Goals card): GOALS ticks / Waiting-on-you exits from a temp brain repo (core.worktree set, cwd inside
   it), dated PROACTIVE/CHANGELOG lines, remind's fired.log, 7/30-day windows, no brain -> nothing invented, counts only.
8. Last night card: night windows (22:00–08:00, named by the morning), ~times, untimed lines, scrub() of names
   (fictional people note) and amounts, plus a real-data sweep of every log-derived line.
10. DST hours with a pinned clock: hour_slots()/collect_tokens() (48 h chart: 02:00 twice in autumn, none in
   spring, tokens in their real hour) and page copies whose timeline labels and quiet band follow Zurich wall time.
7. dash-growth on fixture repos: last commit of a day wins, carry-forward, vault repo preferred, data ignored,
   activity merged from dash-data's daily counts, --if-stale.
"""
import datetime as dt
import glob
import hashlib
import io
import contextlib
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

HOME = os.path.expanduser('~')
DASH = os.environ.get('DASH_BIN', os.path.join(HOME, 'bin', 'dash-data'))   # DASH_BIN/DASH_PAGE_DIR: test a copy before installing
PAGE_DIR = os.environ.get('DASH_PAGE_DIR', os.path.join(HOME, 'opt', 'dash'))
TEMPLATE = os.path.join(PAGE_DIR, 'index.html')   # the data-free page template
if not os.path.exists(TEMPLATE) and 'DASH_PAGE_DIR' not in os.environ:   # checkout / CI: the repo's web/dash/index.html
    TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))), 'web', 'dash', 'index.html')
WWW = os.environ.get('DASH_WWW', os.path.join(HOME, '.local', 'state', 'dash', 'www'))   # DASH_WWW: the generated site
PAGE = os.path.join(WWW, 'index.html')            # template + inlined fallback data (what dash-publish copies)
FRAG = os.path.join(WWW, 'dashboard.html')
# Synthetic profile for the fixture runs (tests never give dash-data the real one): repo layout tests/profile,
# live layout ~/opt/profile-tests. The live-install checks (real data) run with the environment as it is.
FIXTURE_PROFILE = None
for _pf in ('profile', 'profile-tests'):
    _pf = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _pf, 'fixtures', 'profile.json')
    if os.path.isfile(_pf):
        FIXTURE_PROFILE = _pf
        break
REPORTS = os.path.join(HOME, 'reports', 'dash')
FAILS = []
PASSES = 0


def ok(cond, name):
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(name)
        print('FAIL', name)


def load_module():
    loader = importlib.machinery.SourceFileLoader('dashdata', DASH)
    spec = importlib.util.spec_from_loader('dashdata', loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for k, v in o.items():
            yield str(k)
            yield from strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from strings(v)


# ------------------------------------------------------------------------------------------- 1. clean()
def test_assistant_helpers(m):
    ok(m.is_private('---\nname: x\nprivate: true\n---\nbody') and m.is_private('---\nname: x\ndashboard: hide\n---\n'),
       'private skills recognised')
    ok(not m.is_private('---\nname: x\ndescription: private data stays local\n---\nprivate: true'), 'only frontmatter flags count')
    ok(all(m.RX_CHECK.match(l) for l in ('    assert x == 1', '  self.assertEqual(a, b)', 'ok(x, "y")', 'eq "$a" b',
                                           '  expect(x).toBe(1)', 'assert.equal(a, b)')), 'test check lines recognised')
    ok(not any(m.RX_CHECK.match(l) for l in ('def ok(cond, name):', '# assert nothing', 'okay = 1')), 'non-check lines ignored')
    tmp = tempfile.mkdtemp(prefix='dash-loc-')
    try:
        for n in ('a.py', 'b.py'):
            with open(os.path.join(tmp, n), 'w') as f:
                f.write('x = 1\n\n# c\nassert x\n')
        with open(os.path.join(tmp, 'tool'), 'w') as f:
            f.write('#!/bin/bash\necho hi\n')
        langs = dict((os.path.basename(p), lg) for p, lg in m.code_files(tmp))
        ok(langs == {'a.py': 'Python', 'b.py': 'Python', 'tool': 'Shell'}, 'languages from extension and shebang')
        seen = set()
        ok(m.file_lines(os.path.join(tmp, 'a.py'), True, seen) == (3, 1) and m.file_lines(os.path.join(tmp, 'b.py'), True, seen) == (0, 0),
           'non-blank lines counted, identical copies once')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    leaf = {'a': ['fine', 'ghp_' + 'A' * 36], 'b': {'c': 'ok too'}}
    m.redact_strings(leaf)
    ok(leaf['a'][0] == 'fine' and 'A' * 20 not in leaf['a'][1] and leaf['b']['c'] == 'ok too', 'assistant strings get a redact pass')


def test_clean(m):
    c = m.clean
    cases = {
        'Examplestrasse 1, 1234 Exampletown': ['Examplestrasse', '39,'],
        'mail alex.test@example.com now': ['@', 'example.com'],
        'call +41 79 123 45 67 today': ['123 45 67'],
        'call 079 123 45 67 today': ['123 45 67'],
        'IBAN CH93 0076 2011 6238 5295 7 ok': ['0076 2011'],
        'AHV 756.1234.5678.97': ['1234.5678'],
        'chat 100000001 id': ['100000001'],
        'host 198.51.100.5:8000 up': ['192.168'],
        'see https://x.example/a?token=abc123 here': ['token=abc123'],
        'Check-out at Via Luigi Sacco 5 by 10': ['Sacco 5'],
        'password: hunter2 and token=abc': ['hunter2'],
        'key sk-ant-api03-AbCdEf0123456789AbCdEf0123456789xyz': ['AbCdEf0123456789AbCdEf0123456789'],  # secretscan:allow (planted fake)
    }
    for src, banned in cases.items():
        out = c(src)
        for b in banned:
            ok(b not in out, f'clean() masks {b!r} in {src!r} -> {out!r}')
    ok(c('Build privacy vault container') == 'Build privacy vault container', 'clean() keeps plain titles')
    ok(c('Exampletown') == 'Exampletown', 'clean() keeps town names')
    ok(c('x' * 200, 50).endswith('…') and len(c('x' * 200, 50)) <= 50, 'clean() shortens')
    ok(m.cron_human(['*/5', '*', '*', '*', '*']) == 'every 5 min', 'cron every 5 min')
    ok(m.cron_human(['7', '9', '*', '*', '1']) == 'Mon 09:07', 'cron weekly')
    ok(m.cron_human(['13', '8', '*', '*', '*']) == 'daily 08:13', 'cron daily')
    ok(m.cron_human(['23', '9', '1', '*', '*']) == 'monthly on day 1, 09:23', 'cron monthly')
    ok(m.cron_human(['5', '8-22/2', '*', '*', '*']) == 'every 2 h, 08:05–22:05', 'cron stepped hours')
    t = m.title_of('PRIVACY VAULT (owner-approved, levels 1+2): new container `claude-vault` on Unraid (compose x); more')
    ok(t == 'PRIVACY VAULT: new container claude-vault on Unraid', f'title_of: {t!r}')



# ------------------------------------------------------------------------------------------- 1b. names, amounts, nights
def test_lastnight(m):
    tmp = tempfile.mkdtemp(prefix='dash-ln-')
    try:
        note = os.path.join(tmp, 'people.md')
        with open(note, 'w') as f:
            f.write('---\nname: people\n---\nFamily:\n- Mirela "Mira" Testova (Oct 3)\n- Kasimir Plonk: organizes the trip\n'
                    '- Ottilie, his neighbour\n')
        m.PEOPLE_NOTE, m._PEOPLE = note, None
        first, sur = m.people()
        ok({'Mirela', 'Mira', 'Kasimir', 'Ottilie'} <= first and sur == {'Testova', 'Plonk'} and 'Family' not in first and 'Oct' not in first,
           f'people(): first names and surnames from the note ({sorted(first)}, {sorted(sur)})')
        sc = m.scrub
        cases = [
            ('paid CHF 120 to Mirela Testova', 'paid [amount] to someone'),
            ("Kasimir's gift idea, Ottilie asked", "someone's gift idea, someone asked"),
            ('Kasimir Plonk', 'someone'),
            ('best 22.57 CHF/TB, premium 375.00 in 2027', 'best [amount]/TB, premium [amount] in 2027'),
            ('~$0.09/run, ~$15–20/day, €1.2k, Fr. 80.–, 120.– cash', '~[amount]/run, ~[amount]/day, [amount], [amount], [amount] cash'),
            ("rent 1'250.50 and fee: 35", 'rent [amount] and fee: [amount]'),
            ('enable 16.10 09:05, 4.95 TB, 65.25%, CC 2.1.299, mod 0.3.1, 29/29, −1.3k lines, costs 3 h, cost ~1.1M tokens',
             'enable 16.10 09:05, 4.95 TB, 65.25%, CC 2.1.299, mod 0.3.1, 29/29, −1.3k lines, costs 3 h, cost ~1.1M tokens'),
            ("Unraid's restart, The World's End, Jule's invite", "Unraid's restart, The World's End, someone's invite"),
        ]
        for src, want in cases:
            got = sc(src, {'Unraid'})
            ok(got == want, f'scrub({src!r}) -> {got!r}, want {want!r}')
        ok(sc('call Ottilie, Testova later', {'Ottilie', 'Testova'}) == 'call Ottilie, someone later',
           'scrub: a first name already shown elsewhere stays, a surname never')

        ws = os.path.join(tmp, 'ws')
        os.makedirs(ws)
        long_ = 'Built a very long thing ' + 'with many words ' * 12
        with open(os.path.join(ws, 'PROACTIVE.md'), 'w') as f:
            f.write('# Proactive log\n- 2026-10-01 02:00 too old\n- 2026-10-07 02:00 ninth night back\n- 2026-10-08 03:00 oldest shown\n'
                    '- 2026-10-14 ~02:30 approx time\n- 2026-10-14 03:00 — paid CHF 120 to Mirela Testova\n'
                    '- 2026-10-14 21:59 day item\n- 2026-10-14 22:00 evening item\n- 2026-10-15 01:00 — out of order\n'
                    '- 2026-10-15 00:10 started: night build\n- 2026-10-15 — untimed line\n- 2026-10-15 02:00 ' + long_ + '\n'
                    '- 2026-10-15 07:59 last one\n- 2026-10-15 08:00 morning report\n- 2026-10-15 22:10 next night\n')
        m.WORKSPACE = ws
        Z = ZoneInfo('Europe/Zurich')
        ln = m.lastnight({'Unraid'}, datetime(2026, 10, 15, 3, 50, tzinfo=Z))
        t = ln['tonight']
        ok(t['date'] == '2026-10-15' and t['in_progress'], f'lastnight 03:50: tonight is the 15th, in progress ({t["date"]})')
        ok([e['time'] for e in t['items']] == ['22:00', '00:10', '01:00', '02:00', '07:59'],
           f'lastnight: 22:00..07:59 window, sorted ({[e["time"] for e in t["items"]]})')
        ok(t['items'][1]['kind'] == 'started' and t['items'][1]['text'] == 'night build', 'lastnight: kind split off')
        ok(len(t['items'][3]['text']) <= 90 and t['items'][3]['text'].endswith('…'), 'lastnight: items shortened to 90 chars')
        ok([n['date'] for n in ln['earlier']] == ['2026-10-%02d' % d for d in range(14, 7, -1)], 'lastnight: the 7 nights before')
        n14 = ln['earlier'][0]
        ok([e['text'] for e in n14['items']] == ['approx time', 'paid [amount] to someone'], f'lastnight: ~time parsed, scrubbed ({n14["items"]})')
        ok(ln['earlier'][-1]['count'] == 1 and ln['untimed'] == 1, f'lastnight: oldest night kept, untimed counted ({ln["untimed"]})')
        ok(not any('too old' in e['text'] or 'ninth' in e['text'] or 'day item' in e['text'] or 'morning' in e['text']
                   for n in [t] + ln['earlier'] for e in n['items']), 'lastnight: day items and old nights left out')
        ln = m.lastnight(set(), datetime(2026, 10, 15, 12, 0, tzinfo=Z))
        ok(ln['tonight']['date'] == '2026-10-15' and not ln['tonight']['in_progress'], 'lastnight at noon: last night, done')
        ln = m.lastnight(set(), datetime(2026, 10, 15, 22, 30, tzinfo=Z))
        ok(ln['tonight']['date'] == '2026-10-16' and ln['tonight']['in_progress'] and ln['tonight']['count'] == 1,
           'lastnight 22:30: the new night has begun')
        ln = m.lastnight(set(), datetime(2026, 10, 14, 23, 0, tzinfo=Z))
        ok(ln['tonight']['date'] == '2026-10-15' and ln['tonight']['in_progress'] and ln['tonight']['count'] >= 1,
           'lastnight 23:00 with an item: tonight shown')
        ln = m.lastnight(set(), datetime(2026, 10, 11, 22, 30, tzinfo=Z))
        ok(ln['tonight']['date'] == '2026-10-11' and not ln['tonight']['in_progress'] and len(ln['earlier']) == 7,
           f'lastnight: an empty night just begun falls back to the last one ({ln["tonight"]["date"]})')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        m.PEOPLE_NOTE, m._PEOPLE = os.environ.get('DASH_PEOPLE', m.PEOPLE_NOTE), None


def test_clock_edges(m):
    """night_of()/lastnight() with a pinned now (a parameter) at Europe/Zurich DST nights, month end, year end, a
    leap day and every weekday. PROACTIVE.md lines carry wall-clock times, so a night is 22:00..07:59 wall time."""
    tmp = tempfile.mkdtemp(prefix='dash-clock-')
    saved = m.WORKSPACE
    try:
        Z, D = ZoneInfo('Europe/Zurich'), dt.date
        no = m.night_of
        ok(no(D(2026, 12, 31), '22:00') == D(2027, 1, 1) and no(D(2026, 10, 31), '22:15') == D(2026, 11, 1)
           and no(D(2028, 2, 28), '23:00') == D(2028, 2, 29) and no(D(2028, 2, 29), '22:00') == D(2028, 3, 1)
           and no(D(2027, 3, 28), '03:00') == D(2027, 3, 28) and no(D(2026, 10, 25), '12:00') is None,
           'night_of: evening lines roll over month end, year end and the leap day')
        ws = os.path.join(tmp, 'ws')
        os.makedirs(ws)
        log = ['2027-03-27 22:30 spring a', '2027-03-28 01:59 spring b', '2027-03-28 03:00 spring c', '2027-03-28 07:59 spring d',
               '2026-10-24 23:00 fall a', '2026-10-25 02:30 fall b', '2026-10-25 07:00 fall c',
               '2026-10-31 22:15 month a', '2026-11-01 01:00 month b', '2026-12-31 22:00 year a', '2027-01-01 00:00 year b',
               '2028-02-28 23:00 leap a', '2028-02-29 06:00 leap b']
        with open(os.path.join(ws, 'PROACTIVE.md'), 'w') as f:
            f.write('# Proactive log\n' + ''.join(f'- {x}\n' for x in log))
        m.WORKSPACE = ws
        cases = [  # now, tonight, in progress, item texts
            (datetime(2027, 3, 27, 23, 0, tzinfo=Z), '2027-03-28', True, ['spring a', 'spring b', 'spring c', 'spring d']),
            (datetime(2027, 3, 28, 3, 30, tzinfo=Z), '2027-03-28', True, ['spring a', 'spring b', 'spring c', 'spring d']),
            (datetime(2027, 3, 28, 12, 0, tzinfo=Z), '2027-03-28', False, ['spring a', 'spring b', 'spring c', 'spring d']),
            (datetime(2026, 10, 25, 2, 30, tzinfo=Z), '2026-10-25', True, ['fall a', 'fall b', 'fall c']),
            (datetime(2026, 10, 25, 2, 30, fold=1, tzinfo=Z), '2026-10-25', True, ['fall a', 'fall b', 'fall c']),
            (datetime(2026, 10, 31, 22, 30, tzinfo=Z), '2026-11-01', True, ['month a', 'month b']),
            (datetime(2026, 12, 31, 23, 30, tzinfo=Z), '2027-01-01', True, ['year a', 'year b']),
            (datetime(2027, 1, 1, 9, 0, tzinfo=Z), '2027-01-01', False, ['year a', 'year b']),
            (datetime(2028, 2, 28, 22, 30, tzinfo=Z), '2028-02-29', True, ['leap a', 'leap b']),
            (datetime(2028, 2, 29, 12, 0, tzinfo=Z), '2028-02-29', False, ['leap a', 'leap b']),
        ]
        for now, night, live, texts in cases:
            ln = m.lastnight(set(), now)
            t = ln['tonight']
            back = [(D.fromisoformat(night) - dt.timedelta(days=k)).isoformat() for k in range(1, 8)]
            ok(t['date'] == night and t['in_progress'] == live and [e['text'] for e in t['items']] == texts
               and [n['date'] for n in ln['earlier']] == back,
               f'lastnight at {now:%Y-%m-%d %H:%M %Z}: night {t["date"]}, live {t["in_progress"]}, '
               f'{[e["text"] for e in t["items"]]}, earlier {ln["earlier"][0]["date"]}..{ln["earlier"][-1]["date"]}')
        for k in range(7):  # Mon 19 .. Sun 25 Oct 2026 (the 25th is the fall-back Sunday)
            day = D(2026, 10, 19) + dt.timedelta(days=k)
            noon = m.lastnight(set(), datetime(day.year, day.month, day.day, 12, 0, tzinfo=Z))['tonight']
            late = m.lastnight(set(), datetime(day.year, day.month, day.day, 23, 30, tzinfo=Z))['tonight']
            ok(noon['date'] == day.isoformat() and not noon['in_progress'] and late['date'] in (day.isoformat(),
               (day + dt.timedelta(days=1)).isoformat()), f'lastnight on {day:%a %d.%m}: noon -> {noon["date"]}, 23:30 -> {late["date"]}')
    finally:
        m.WORKSPACE = saved
        shutil.rmtree(tmp, ignore_errors=True)


def test_goals(m):
    root = tempfile.mkdtemp(prefix='dash-goals-')
    try:
        g = os.path.join(root, 'GOALS.md')
        with open(g, 'w') as f:
            f.write('# Goals (draft)\n\nIntro line\n- [ ] not under a goal, ignored\n\n'
                    '## 1. Admin on autopilot (his top annoyance, confirmed 2026-10-09)\nDone = x.\n'
                    '- [x] Bills tracked\n- [ ] Bills via Gmail (blocked: vault Gmail)\n'
                    '- [ ] Call +41 79 555 66 77 at Musterweg 12 about the lease; then more text here\n'
                    '## 2. Money\n- [ ] Waiting on the owner for the token\n  - [x] nested done step\n'
                    '## Untitled numbering\n')
        r = m.parse_goals(g)
        gs = r['goals']
        ok([x['n'] for x in gs] == [1, 2, 3] and gs[0]['title'] == 'Admin on autopilot', f'goals parsed, parens dropped: {[x["title"] for x in gs]}')
        ok((gs[0]['done'], gs[0]['open'], gs[0]['total'], gs[0]['blocked']) == (1, 2, 3, 1), f'goal 1 counts: {gs[0]}')
        ok(gs[0]['next'] and not gs[0]['next_blocked'] and gs[0]['next'].startswith('Call'), 'next step skips blocked steps')
        ok('555 66 77' not in gs[0]['next'] and 'Musterweg' not in gs[0]['next'], f'next step masked: {gs[0]["next"]!r}')
        ok(gs[1]['next_blocked'] and gs[1]['done'] == 1, 'only blocked steps left -> next_blocked; nested steps count')
        ok(gs[2]['total'] == 0 and gs[2]['pct'] == 0, 'goal without steps')
        ok((r['done'], r['open'], r['total']) == (2, 3, 5), f'goal totals: {r["done"]}/{r["total"]}')
        ok(m.parse_goals(os.path.join(root, 'missing.md'))['goals'] == [], 'missing GOALS.md -> no goals')
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_done_metrics(m):
    """done_metrics(): GOALS ticks and Waiting-on-you exits from a temp brain repo's history + the live files,
    PROACTIVE/CHANGELOG dated lines, remind's fired.log; rolling 7/30 days; counts only."""
    root = tempfile.mkdtemp(prefix='dash-done-')
    saved = (m.BRAIN_GIT, m.WORKSPACE, m.REMIND_FIRED)
    try:
        ws = os.path.join(root, 'workspace')
        os.makedirs(ws)
        now = m.now_local()
        day = lambda n, hm=None: (now - dt.timedelta(days=n)).strftime('%Y-%m-%d') + (' ' + hm if hm else '')  # noqa: E731
        genv = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
        subprocess.run(['git', 'init', '-q', root], check=True, env=genv)
        subprocess.run(['git', '-C', root, 'config', 'core.worktree', root], check=True, env=genv)   # like the brain (~)

        def put(name, text):
            with open(os.path.join(ws, name), 'w') as f:
                f.write(text)

        def commit(days_ago, goals, todo):
            put('GOALS.md', goals)
            put('TODO.md', todo)
            when = '@%d +0000' % int(now.timestamp() - days_ago * 86400)
            e = dict(genv, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
            subprocess.run(['git', '-C', root, 'add', '-A'], check=True, env=e)
            subprocess.run(['git', '-C', root, 'commit', '-q', '--allow-empty', '-m', 'snap'], check=True, env=e)

        G = '# Goals\n## 1. Admin\n- [{a}] Alpha step one done\n- [{b}] Beta step two here\n## 2. Money\n- [{c}] Gamma step {cx}\n{d}'
        T = '# TODO\n## In progress\n- **Busy** — x\n## Waiting on you\n{w}\n## Ideas\n- **Idea** — y\n'
        w = lambda *ts: '\n'.join('- **%s** — asked' % t for t in ts)  # noqa: E731
        commit(40, G.format(a='x', b=' ', c=' ', cx='three', d=''), T.format(w=w('Xavier lease question', 'Disk day plan')))
        commit(20, G.format(a='x', b='x', c=' ', cx='three', d=''), T.format(w=w('Disk day plan')))
        commit(3, G.format(a='x', b='x', c='x', cx='three', d='- [x] Delta added already done\n'),
               T.format(w=w('Disk check after preclear', 'Zebra crossing note', 'Quokka return item')))
        commit(2, G.format(a='x', b='x', c=' ', cx='three', d='- [x] Delta added already done\n'),
               T.format(w=w('Disk check after preclear', 'Walrus new question')))
        # live (uncommitted): Gamma re-ticked and reworded after its first 4 words, Alpha's text edited; Quokka back
        put('GOALS.md', G.format(a='x', b='x', c='x', cx='three (16.10, done)', d='- [x] Delta added already done\n')
            .replace('Alpha step one done', 'Alpha step one done (12.10, details)'))
        put('TODO.md', T.format(w=w('Disk check after preclear', 'Walrus new question', 'Quokka return item')))
        put('PROACTIVE.md', '# Proactive log\nintro - 2020-01-01 not a line\n\n- %s — started: thing\n- %s — thing done\n- %s — older\n'
            '- %s — too old\n- %s untimed today\n' % (day(1, '10:00'), day(1, '11:00'), day(10, '09:00'), day(40, '09:00'), day(0)))
        put('CHANGELOG.md', '# Changelog\n\n- %s: built a\n- %s %s built b\n- %s built c\n- %s built d\n'
            % (day(2), day(5), '08:00', day(25), day(31, '08:00')))
        fired = os.path.join(root, 'fired.log')
        with open(fired, 'w') as f:
            f.write('%s owner\n%s me\nnot a line\n%s me\n' % (day(29, '08:00'), day(8, '08:00'), day(1, '08:00')))
        m.BRAIN_GIT, m.WORKSPACE, m.REMIND_FIRED = os.path.join(root, '.git'), ws, fired
        goals = m.parse_goals(os.path.join(ws, 'GOALS.md'))['goals']
        cwd = os.getcwd()
        os.chdir(ws)   # cwd inside the work tree (cron/session cwd ~/workspace): paths must not resolve against it
        try:
            r = m.done_metrics(goals, now)
        finally:
            os.chdir(cwd)
        ok(r['goal_steps'] == {'d7': 2, 'd30': 3}, f'goal steps ticked: Gamma+Delta in 7 d, +Beta in 30 d, Alpha before, re-tick once: {r["goal_steps"]}')
        ok([(g['n'], g['ticked_7d'], g['ticked_30d']) for g in goals] == [(1, 0, 1), (2, 2, 2)], f'per-goal ticks: {goals}')
        ok(r['answered'] == {'d7': 1, 'd30': 2}, f'answered: Xavier (20 d), Zebra (2 d); Disk day->Disk check is one item, Quokka is back: {r["answered"]}')
        ok(r['waiting_now'] == 3, f'still waiting = live Waiting count: {r["waiting_now"]}')
        ok(r['todo_done'] == {'d7': 2, 'd30': 3}, f'tasks finished: dated PROACTIVE lines minus started: {r["todo_done"]}')
        ok(r['built'] == {'d7': 2, 'd30': 3}, f'things built: dated CHANGELOG lines, both formats: {r["built"]}')
        ok(r['reminders'] == {'d7': 1, 'd30': 3} and r['reminders_since'] == day(29), f'reminders fired: {r["reminders"]} since {r["reminders_since"]}')
        words = set(re.findall(r'[A-Za-z]{4,}', ' '.join(strings(r))))
        ok(not words & {'Xavier', 'Disk', 'Zebra', 'Quokka', 'Walrus', 'Alpha', 'Beta', 'Gamma', 'Delta', 'older', 'untimed'},
           f'done metrics carry counts only, no titles: {sorted(words)}')
        # no brain repo / no fired log: the live files are the baseline (nothing counted as ticked or answered)
        m.BRAIN_GIT, m.REMIND_FIRED = os.path.join(root, 'nope.git'), os.path.join(root, 'nope.log')
        r = m.done_metrics(m.parse_goals(os.path.join(ws, 'GOALS.md'))['goals'], now)
        ok(r['goal_steps'] == {'d7': 0, 'd30': 0} and r['answered'] == {'d7': 0, 'd30': 0} and r['waiting_now'] == 3,
           f'no brain history: no ticks/answers invented: {r}')
        ok(r['reminders'] is None and r['reminders_since'] is None, 'no fired.log -> reminders null')
        g = m.goals_block()
        ok(isinstance(g.get('done_metrics'), dict) and g['done_metrics']['built']['d30'] == 3, 'goals_block() ships done_metrics')
        # injected bug (deliberate ZeroDivisionError): its stderr line is captured, so the suite log shows no false alarm
        m.done_metrics, keep = (lambda *a, **k: 1 / 0), m.done_metrics
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                g = m.goals_block()
        finally:
            m.done_metrics = keep
        ok(g['done_metrics'] is None and g['goals'], 'a done_metrics bug fails soft (card keeps its goals)')
        ok('done metrics failed: ZeroDivisionError' in err.getvalue(), f'the fail-soft path names the error: {err.getvalue()!r}')
    finally:
        m.BRAIN_GIT, m.WORKSPACE, m.REMIND_FIRED = saved
        shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------------------------------------------------- 2. fixtures
PLANTED = ['Musterweg 12', 'planted.person@example.org', 'sk-ant-api03-PLANTEDsecretPLANTEDsecret0123456789',  # secretscan:allow (planted fakes)
           'Top secret family conflict', '+41 79 555 66 77']


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def make_fixture(root):
    import time
    # 1 h ago, but never before today's Zurich midnight (else 'today' checks fail just after 00:00)
    _z = datetime.now(ZoneInfo('Europe/Zurich'))
    now = max(time.time() - 3600, _z.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + 1)
    proj = os.path.join(root, 'projects', '-home-node-workspace')
    os.makedirs(os.path.join(proj, 'S1', 'subagents'))
    secret_text = ' '.join(PLANTED)

    def asst(mid, ts, model, inp, out, cr, cw1h, tools=(), side=False):
        return {'type': 'assistant', 'isSidechain': side, 'timestamp': iso(ts), 'version': '9.9.9',
                'message': {'id': mid, 'model': model, 'role': 'assistant',
                            'content': [{'type': 'text', 'text': secret_text}] +
                                       [{'type': 'tool_use', 'id': f'{mid}-t{i}', 'name': n, 'input': {'cmd': secret_text}} for i, n in enumerate(tools)],
                            'usage': {'input_tokens': inp, 'output_tokens': out, 'cache_read_input_tokens': cr,
                                      'cache_creation_input_tokens': cw1h,
                                      'cache_creation': {'ephemeral_1h_input_tokens': cw1h, 'ephemeral_5m_input_tokens': 0}}}}

    main = [
        {'type': 'user', 'timestamp': iso(now), 'origin': {'kind': 'channel', 'server': 'plugin:telegram-plus:telegram'},
         'message': {'role': 'user', 'content': '<channel source="plugin:telegram-plus:telegram">' + secret_text + '</channel>'}},
        asst('m1', now + 1, 'claude-opus-5-5', 10, 1000, 100000, 2000, tools=['Bash', 'mcp__plugin_telegram-plus_telegram__reply']),
        asst('m1', now + 1, 'claude-opus-5-5', 10, 1000, 100000, 2000),   # duplicate content block of the same message
        {'type': 'user', 'timestamp': iso(now + 2), 'origin': {'kind': 'human'},
         'message': {'role': 'user', 'content': '[heartbeat] Idle 12 min'}},
        {'type': 'user', 'timestamp': iso(now + 3), 'message': {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'm1-t0', 'content': secret_text}]}},
        asst('m2', now + 4, 'claude-opus-5-5', 0, 500, 0, 0),
        {'type': 'cost-state', 'startTime': 1, 'totalCostUSD': 1.5, 'modelUsage': {}},
    ]
    with open(os.path.join(proj, 'S1.jsonl'), 'w') as f:
        for r in main:
            f.write(json.dumps(r) + '\n')
    # forked copy of the main session repeats m1 (must be deduped)
    with open(os.path.join(proj, 'S0.jsonl'), 'w') as f:
        f.write(json.dumps(main[1]) + '\n')
    sub = [asst('s1', now + 10, 'claude-sonnet-5-5', 100, 2000, 50000, 1000, tools=['Read'], side=True),
           asst('s2', now + 70, 'claude-sonnet-5-5', 0, 1000, 0, 0, side=True)]
    with open(os.path.join(proj, 'S1', 'subagents', 'agent-abc123def456.jsonl'), 'w') as f:
        for r in sub:
            f.write(json.dumps(r) + '\n')
    with open(os.path.join(proj, 'S1', 'subagents', 'agent-abc123def456.meta.json'), 'w') as f:
        json.dump({'agentType': 'general-purpose', 'description': 'Fixture run at Musterweg 12', 'effort': 'xhigh',
                   'requestShape': 'background', 'spawnDepth': 1}, f)
    today = _z.strftime('%Y-%m-%d')
    ws = os.path.join(root, 'workspace')
    os.makedirs(ws)
    with open(os.path.join(ws, 'GOALS.md'), 'w') as f:
        f.write('## 1. Fixture goal\n- [x] first step\n- [ ] second step at Musterweg 12\n')
    with open(os.path.join(ws, 'PROACTIVE.md'), 'w') as f:
        f.write(f'- 2000-01-01 10:00 done: ancient entry\n- {today} 09:15 done: built fixture thing; mail planted.person@example.org\n'
                f'- {today} 10:30 started: second fixture thing\n')
    with open(os.path.join(ws, 'CHANGELOG.md'), 'w') as f:
        f.write(f'- {today} — fixture change at Musterweg 12\n- 2000-01-01 — old change\n')
    with open(os.path.join(ws, 'TODO.md'), 'w') as f:
        f.write('# TODO\n## In progress\n- **Fixture task** — running\n')
    aw = os.path.join(root, 'aw')
    os.makedirs(aw)
    with open(os.path.join(aw, 'agent-watch.log'), 'w') as f:
        f.write(f'{today} 09:00:01 aaaa running -> done\n{today} 09:05:01 progress bbbb sent (14 min)\n'
                f'{today} 09:10:01 stalled cccc silent 16m -> inbox/x.md\n2000-01-01 09:00:01 dddd running -> done\n')
    with open(os.path.join(aw, 'state.json'), 'w') as f:
        json.dump({'agents': {'a1': {'run_start': now, 'status': 'done'}, 'a2': {'run_start': 946720800, 'status': 'done'}}}, f)
    ctx = {'schema': 1, 'generated_at': _z.isoformat(timespec='seconds'), 'date': today,
           'sources': [{'name': 'hn', 'status': 'ok', 'public': '2 picks from 30 front-page stories'},
                       {'name': 'location', 'status': 'ok', 'public': 'Home Assistant, updated 5 min ago'},
                       {'name': 'telegram', 'status': 'ok', 'public': '3 chats summarized'},
                       {'name': 'weather', 'status': 'error', 'public': 'x', 'error': 'URLError at https://x.example/a?token=abc123'},
                       {'name': 'odd', 'status': 'weird-status', 'public': 'y'}],
           'server': {'health': '1 critical, 3 warning(s); new since last run: 0 (report 2026-10-15 08:13)',
                      'capacity': 'array 91.0% used, 8.5 TB free; full ~2026-11-21 (in 38 d at the 30-day trend)', 'media': 'x'},
           'hn': {'picks': [{'id': 123, 'title': 'Claude agents on Unraid', 'points': 42, 'domain': 'example.com', 'tags': ['AI', 'evil-tag'],
                             'url': 'https://example.com/post?ref=PLANTEDURL'},
                            {'id': 'bad'}]},
           'agenda': {'reminders': [{'at': today + ' 10:00', 'text': 'PLANTEDREMINDER for PersonB'}]},
           'location': {'status': 'ok', 'place': 'PLANTEDPLACE', 'lat': 47.5000, 'lon': 8.5000},
           'telegram': {'status': 'ok', 'chats': 3, 'summary': 'PLANTEDTGCONTENT'}}
    with open(os.path.join(root, 'context.json'), 'w') as f:
        json.dump(ctx, f)
    jobs = os.path.join(root, 'jobs', 'demo')
    os.makedirs(jobs)
    with open(os.path.join(jobs, 'run.jsonl'), 'w') as f:
        f.write(json.dumps(asst('j1', now + 20, 'claude-sonnet-5-5', 6, 30, 0, 100)) + '\n')
        f.write(json.dumps({'type': 'result', 'total_cost_usd': 0.0123, 'result': secret_text,
                            'modelUsage': {'claude-sonnet-5-5': {'inputTokens': 6, 'outputTokens': 386,
                                                                  'cacheReadInputTokens': 23361, 'cacheCreationInputTokens': 15220}}}) + '\n')


def fixture_env(root, extra_env=None):
    dfm = os.path.join(root, 'dataflow.json')   # synthetic data map; the curated live one is not in a checkout
    if not os.path.exists(dfm):
        with open(dfm, 'w') as f:
            json.dump({'nodes': [], 'stores': [], 'flows': []}, f)
    env = dict(os.environ, DASH_DATAFLOW=dfm, DASH_PROJECTS=os.path.join(root, 'projects'), DASH_JOBS=os.path.join(root, 'jobs'),
               DASH_STATE=os.path.join(root, 'state'), DASH_PAGE_DIR=os.path.join(root, 'page'),
               DASH_WORKSPACE=os.path.join(root, 'workspace'),
               DASH_DAYLINE=os.path.join(root, 'state', 'dayline.json'), DAYLINE_AW_DIR=os.path.join(root, 'aw'),
               DASH_CONTEXT=os.path.join(root, 'context.json'), DASH_VAULT='/bin/false', DASH_VAULT_TTL='0',
               DASH_BROWSER='/bin/false', DASH_WHISPER_CTL='/bin/false',   # health(): no real Chrome/whisper
               DASH_PHONE='/bin/false', DASH_VOICE='/bin/false', DASH_WEBAGENT='/bin/false', DASH_ABX='/bin/false',
               DASH_HA='/bin/false', DASH_SYS_TIMEOUT='3',   # systems(): no real gateway/relay/web agent/HA reads
               DASH_BROWSER_HOST=os.path.join(root, 'no-browser-host'), DASH_VOICE_OFF=os.path.join(root, 'no-voice-off'),
               DASH_PHONEGW_OFF=os.path.join(root, 'no-phonegw-off'), DASH_SCHEDULES=os.path.join(root, 'no-schedules.toml'),
               DASH_BRAIN_GIT=os.path.join(root, 'no-brain.git'), DASH_REMIND_FIRED=os.path.join(root, 'no-fired.log'))
    env.pop('DASH_WWW', None)
    if FIXTURE_PROFILE:
        env['ASSISTANT_PROFILE'] = FIXTURE_PROFILE
    env.update(extra_env or {})
    return env


def run_fixture(root, extra_env=None):
    return subprocess.run([DASH, '--stdout'], capture_output=True, text=True, env=fixture_env(root, extra_env), timeout=120)


def test_split():
    """dash-data on fixtures (not --stdout): the template is only read; www/ gets the page with the data inlined,
    data.json and (--inline) the artifact fragment; {{network.server_name}} comes from the fixture profile and a
    token outside dash-data's allowlist stays as it is."""
    root = tempfile.mkdtemp(prefix='dash-split-')
    try:
        make_fixture(root)
        os.makedirs(os.path.join(root, 'page'), exist_ok=True)
        tpl = ('<!doctype html><html><head><!--dash:head--><title>Claude Bot Dashboard</title><!--/dash:head--></head><body>'
               '<!--dash:body--><svg><text>Unraid server · {{network.server_name}}</text><text>{{home.street}}</text></svg>'
               '<script id="dash-data" type="application/json" data-mode="fallback">{}</script><!--/dash:body--></body></html>\n')
        tp = os.path.join(root, 'page', 'index.html')
        with open(tp, 'w') as f:
            f.write(tpl)
        r = subprocess.run([DASH, '--inline'], capture_output=True, text=True, env=fixture_env(root), timeout=120)
        ok(r.returncode == 0, f'split: dash-data --inline on fixtures exits 0 ({r.stderr[-300:]})')
        www = os.path.join(root, 'state', 'www')
        ok(open(tp).read() == tpl, 'split: the template is left untouched')
        ok(sorted(os.listdir(os.path.join(root, 'page'))) == ['index.html'], 'split: nothing written next to the template')
        gen = open(os.path.join(www, 'index.html')).read() if os.path.exists(os.path.join(www, 'index.html')) else ''
        m = re.search(r'<script id="dash-data" type="application/json" data-mode="fallback">(.*?)</script>', gen, re.S)
        cur = json.load(open(os.path.join(root, 'state', 'data.json'))) if r.returncode == 0 else {}
        emb = json.loads(m.group(1).replace('<\\/', '</')) if m else {}
        ok(bool(cur) and emb == cur, 'split: www/index.html carries the data as its fallback copy')
        ok(os.path.exists(os.path.join(www, 'data.json')) and json.load(open(os.path.join(www, 'data.json'))) == cur,
           'split: www/data.json = the data')
        ok('Unraid server · Testsrv</text>' in gen, 'split: {{network.server_name}} filled from the (fixture) profile')
        ok('<text>{{home.street}}</text>' in gen, 'split: a token outside the allowlist is not filled')
        frag = open(os.path.join(www, 'dashboard.html')).read() if os.path.exists(os.path.join(www, 'dashboard.html')) else ''
        ok('data-mode="inline"' in frag and 'Testsrv' in frag and not re.search(r'<!doctype|<html|<head[\s>]|<body', frag, re.I),
           'split: --inline writes the artifact fragment to www/')
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_fixture():
    root = tempfile.mkdtemp(prefix='dash-test-')
    try:
        make_fixture(root)
        r = run_fixture(root)
        ok(r.returncode == 0, f'fixture run exits 0 (stderr: {r.stderr[-300:]})')
        if r.returncode != 0:
            return
        d = json.loads(r.stdout)
        t = d['tokens']
        bk = t['by_kind']
        ok(bk['main']['messages'] == 2, f"main messages deduped to 2 (got {bk['main']['messages']})")
        exp_main = (10 * 4 + 1000 * 20 + 100000 * 0.2 + 2000 * 8 + 500 * 20) / 1e6
        ok(abs(bk['main']['cost_usd'] - exp_main) < 1e-4, f"main cost {bk['main']['cost_usd']} == {exp_main}")
        exp_sub = (100 * 2 + 3000 * 10 + 50000 * 0.1 + 1000 * 4) / 1e6
        ok(abs(bk['subagent']['cost_usd'] - exp_sub) < 1e-4, f"subagent cost {bk['subagent']['cost_usd']} == {exp_sub}")
        ok(abs(bk['headless']['cost_usd'] - 0.0123) < 1e-4, 'headless cost taken from the result record')
        ok(bk['headless']['output'] == 386, 'headless tokens from the result record, not partial stream usage')
        ok(t['totals']['tokens'] == sum(b['tokens'] for b in bk.values()), 'totals = sum of kinds')
        runs = {x['id']: x for x in t['top_runs']}
        ok('abc123de' in runs and runs['abc123de']['effort'] == 'xhigh', 'subagent run listed with its meta')
        ok(runs.get('abc123de', {}).get('minutes') == 1.0, 'subagent run duration 1 min')
        ok('[street]' in runs.get('abc123de', {}).get('description', ''), 'street in a subagent description is masked')
        td = t['today'] or {}
        ok(td.get('turns', {}).get('telegram') == 1 and td.get('turns', {}).get('heartbeat') == 1,
           f"turn classes counted: {td.get('turns')}")
        names = {x['name']: x['calls'] for x in t['tools_top']}
        ok(names.get('telegram: reply') == 1 and names.get('Bash') == 1 and names.get('Read') == 1, f'tool labels: {names}')
        ok(td.get('telegram_replies') == 1, 'telegram replies counted')
        ok(t['claude_code_counter_usd'] == 1.5, 'Claude Code counter read')
        ok(len(t['hourly']) == 48 and sum(sum(h['tokens'].values()) for h in t['hourly']) == t['totals']['tokens'],
           'hourly window holds the fixture tokens')
        # cost per kind per day, cost pace, 24 h timeline (the budget tile was retired 2026-10-15)
        ok(all(abs(sum(x['cost_by_kind'].values()) - x['cost_usd']) < 1e-3 for x in t['daily']), 'daily cost_by_kind adds up to cost_usd')
        ok(abs(td.get('cost_by_kind', {}).get('subagent', 0) - exp_sub) < 1e-4, 'subagent cost lands in cost_by_kind')
        cp = t.get('cost_pace') or {}
        ok(cp.get('days') == 0 and cp.get('avg_usd') is None and abs(cp.get('today_usd', -1) - td.get('cost_usd', 0)) < 1e-6,
           f'cost pace without earlier days: {cp}')
        ok('timeline' not in t, 'timeline moved out of tokens')
        tl = d['activity'].get('timeline') or {}
        kinds_tl = sorted(x['kind'] for x in tl.get('runs', []))
        ok(kinds_tl == ['headless', 'subagent'], f'timeline holds the subagent and worker runs: {kinds_tl}')
        sub_tl = [x for x in tl.get('runs', []) if x['kind'] == 'subagent']
        ok(sub_tl and sub_tl[0]['minutes'] == 1.0 and '[street]' in sub_tl[0]['description'], 'timeline run: duration and masked description')
        ok(all(x['start'] <= x['end'] and tl['from'] <= x['end'] for x in tl.get('runs', [])), 'timeline runs inside the window')
        om = tl.get('owner_messages', [])
        ok(len(om) == 1 and om[0]['via'] == 'telegram' and set(om[0]) == {'at', 'via'}, f'owner messages: times only, heartbeats excluded ({om})')
        ok('budget' not in d, 'no budget block (tile retired 2026-10-15)')
        hc = {c['name']: c for c in d['health']['checks']}
        ok(hc.get('Server (Unraid)', {}).get('status') == 'critical' and hc['Server (Unraid)']['value'] == '1 critical · 3 warnings',
           f"server health from the context snapshot: {hc.get('Server (Unraid)')}")
        ok(hc.get('Array capacity', {}).get('status') == 'warn' and hc['Array capacity']['value'] == '91% used, full in ~38 d',
           f"array capacity check: {hc.get('Array capacity')}")
        ok(d['health']['summary'].get('critical', 0) >= 1 and [c['name'] for c in d['health']['checks']][:2] == ['Server (Unraid)', 'Array capacity'],
           'server checks come first and count in the summary')
        # goals, dayline (live: no dayline.json in the fixture), context card
        gl = d.get('goals') or {}
        ok([x['title'] for x in gl.get('goals', [])] == ['Fixture goal'] and (gl['done'], gl['total']) == (1, 2), f'fixture goals: {gl}')
        ok('[street]' in (gl['goals'][0]['next'] or ''), 'goal next step masked')
        dl = d['activity'].get('dayline') or {}
        ok(dl.get('today') and set(dl) == {'date', 'today', 'done', 'changes'}, f'dayline: today, done lines and a change count: {sorted(dl)}')
        ok([e['text'][:19] for e in dl.get('done', [])] == ['second fixture thin', 'built fixture thing'], f'dayline entries, newest first: {dl.get("done")}')
        ok('[email]' in dl['done'][1]['text'], 'dayline entry masked')
        ok(dl.get('done', [{}])[0].get('kind') == 'started', 'dayline keeps the entry kind')
        ok(dl.get('changes') == 1, f'dayline: count of today\'s setup changes only (the lines are on the Codebase tab): {dl.get("changes")}')
        # file path: a fresh dayline.json for today is used as-is
        with open(os.path.join(root, 'state', 'dayline.json'), 'w') as f:
            json.dump({'date': dt.datetime.now(ZoneInfo('Europe/Zurich')).strftime('%Y-%m-%d'), 'proactive': [{'time': '07:00', 'kind': 'done', 'text': 'from file'}],
                       'changes': [], 'agents': {}}, f)
        df_ = json.loads(run_fixture(root).stdout)['activity']['dayline']
        ok(df_['done'][0]['text'] == 'from file', f'dayline.json used when fresh: {df_}')
        os.utime(os.path.join(root, 'state', 'dayline.json'), (1, 1))
        df_ = json.loads(run_fixture(root).stdout)['activity']['dayline']
        ok(df_['done'][0]['text'] != 'from file', 'stale dayline.json (older than the logs) -> live')
        cx = d.get('context') or {}
        ok([x['id'] for x in cx.get('hn', [])] == [123] and cx['hn'][0]['tags'] == ['AI'], f'context HN picks validated: {cx.get("hn")}')
        ok(set(cx['hn'][0]) == {'id', 'title', 'points', 'domain', 'tags'}, 'context picks carry no URL')
        ok(set(cx) == {'generated_local', 'age_min', 'hn'}, f'context card: HN picks only (sources list retired 2026-10-15): {sorted(cx)}')
        ok('token=abc123' not in r.stdout and '3 chats summarized' not in r.stdout, 'context source notes never in output')
        text = r.stdout
        for p in ['PLANTEDURL', 'PLANTEDREMINDER', 'PLANTEDPLACE', 'PLANTEDTGCONTENT', '47.5000', '8.5000', 'PersonB']:
            ok(p not in text, f'private context field never in output: {p}')
        for p in PLANTED + ['Musterweg']:
            ok(p not in text, f'planted private text never in output: {p[:12]}…')
        # second run reuses the cache and gives the same token numbers
        r2 = run_fixture(root)
        d2 = json.loads(r2.stdout)
        ok(d2['tokens']['totals'] == t['totals'], 'cached run gives identical totals')
        cache = open(os.path.join(root, 'state', 'cache.json')).read()
        for p in PLANTED:
            ok(p not in cache, f'cache never holds message text: {p[:12]}…')
    finally:
        shutil.rmtree(root, ignore_errors=True)



FAKE_VAULT = r"""#!/usr/bin/env python3
import json, sys, datetime as dt
from zoneinfo import ZoneInfo
now = dt.datetime.now(ZoneInfo('Europe/Zurich'))
ago = lambda **k: (now - dt.timedelta(**k)).isoformat(timespec='seconds')
cmd = sys.argv[1]
if cmd == 'audit':
    print(json.dumps([
        {'ts': ago(days=6, hours=12), 'event': 'job_done'},
        {'ts': ago(days=6), 'event': 'raw_request', 'source': 'tg', 'query': 'PLANTEDQUERY words', 'reason': 'PLANTEDREASON'},
        {'ts': ago(hours=50), 'event': 'tg_recent', 'chars': 10},
        {'ts': ago(hours=30), 'event': 'paperless_search'},
        {'ts': ago(hours=10), 'event': 'raw_released', 'source': 'test'},
        {'ts': ago(hours=9), 'event': 'raw_released', 'source': 'tg', 'query': 'PLANTEDQUERY2'},
        {'ts': ago(hours=8), 'event': 'llm_run', 'flags': ['--anonymize']},
        {'ts': ago(hours=7), 'event': 'paperless_meta'},
        {'ts': ago(hours=6), 'event': 'find'},
        {'ts': ago(hours=5), 'event': 'firefly_summary'},
        {'ts': ago(hours=3), 'event': 'paperless_get'},
        {'ts': ago(hours=2), 'event': 'gmail_get'},
        {'ts': ago(hours=1), 'event': 'tg_search'},
        {'ts': 'not a time', 'event': 'tg_search'},
    ]))
elif cmd == 'status':
    print(json.dumps({'vault': {'policy': 'llm-local-v4.1+regex-2026-10-13'}, 'llm': {'model': 'gemma-4-26B-A4B-it-UD-IQ4_XS.gguf', 'healthy': True},
                      'tg': {'logged_in': True, 'live': {'state': 'running'}}, 'gmail': {'configured': True, 'error': None,
                      'accounts': [{'account': 'main'}, {'account': 'personal'}]}, 'paperless': {'configured': True},
                      'firefly': {'configured': False}, 'raw': {'pending': 1}, 'alerts': 0}))
elif cmd == 'metrics':
    print(json.dumps({'routes': [{'route': 'GET /status', 'calls': 10, 'errors': 0}, {'route': 'POST /tg/search', 'calls': 3, 'errors': 2}]}))
else:
    sys.exit(2)
"""


def test_gz_subagent():
    """transcript-compact gzips old subagent transcripts: a .jsonl.gz counts like the plain file (tokens, cost,
    run + meta), and a run present as both .jsonl and .jsonl.gz is counted once."""
    import gzip
    exp_sub = (100 * 2 + 3000 * 10 + 50000 * 0.1 + 1000 * 4) / 1e6
    for both in (False, True):
        root = tempfile.mkdtemp(prefix='dash-test-gz-')
        try:
            make_fixture(root)
            plain = os.path.join(root, 'projects', '-home-node-workspace', 'S1', 'subagents', 'agent-abc123def456.jsonl')
            with open(plain, 'rb') as src, gzip.open(plain + '.gz', 'wb') as dst:
                dst.write(src.read())
            if not both:
                os.remove(plain)
            r = run_fixture(root)
            label = 'plain + gz copies' if both else 'gz only'
            ok(r.returncode == 0, f'gz fixture run exits 0 ({label}; stderr: {r.stderr[-200:]})')
            if r.returncode != 0:
                continue
            d = json.loads(r.stdout)
            sub = d['tokens']['by_kind'].get('subagent', {'cost_usd': 0, 'messages': 0})
            ok(abs(sub['cost_usd'] - exp_sub) < 1e-4, f"gzipped subagent cost counted once ({label}): {sub['cost_usd']} == {exp_sub}")
            ok(sub['messages'] == 2, f"gzipped subagent messages counted once ({label}): {sub['messages']}")
            runs = {x['id']: x for x in d['tokens']['top_runs']}
            ok(runs.get('abc123de', {}).get('effort') == 'xhigh', f'gzipped subagent run keeps its id and meta ({label})')
        finally:
            shutil.rmtree(root, ignore_errors=True)


def test_sys_voice(m):
    """The voice card's 7-day numbers: from the hub relay's voice.status (stats_7d) when it carries them, without
    running the local `stats`; else (a hub release without them) from the local log's `stats`. The relay runs only in
    claude-hub-voice since the v1 sweep (no ~/.voice-relay-off switch any more): `where` is always hub."""
    hub7 = {'calls': 4, 'connected': 3, 'ring': {'answered': 2, 'declined': 1}, 'handoffs': 2, 'errors': 1,
            'reply_ms': {'n': 5, 'median': 900, 'p90': 1500, 'max': 2000}, 'days': 7}
    loc7 = {'calls': 9, 'connected': 9, 'ring': {}, 'reply_ms': {'n': 1, 'median': 111, 'p90': 111, 'max': 111}}
    ran, saved = [], m.jcall
    try:
        def fake(st):
            def jcall(cmd, timeout=None):
                ran.append(cmd[1])
                return st if cmd[1] == 'status' else loc7
            return jcall
        m.jcall = fake({'live': 0, 'via': 'hub', 'calls': [], 'stats_7d': hub7})
        v = m.sys_voice()
        ok(ran == ['status'], f'sys_voice: hub stats_7d used, local stats not run (ran {ran})')
        ok(v['stats_src'] == 'hub' and v['where'] == 'hub' and v['calls_7d'] == 4 and v['connected_7d'] == 3
           and v['reply_ms'] == 900 and v['reply_p90_ms'] == 1500 and v['handoffs'] == 2
           and v['ring'] == {'answered': 2, 'declined': 1, 'failed': 0}, f'sys_voice: hub numbers ({v})')
        ok(v['status'] == 'warn' and '1 problem in 7 d' in v['summary'], f'sys_voice: hub errors count ({v})')
        ran.clear()
        m.jcall = fake({'live': 0, 'via': 'hub', 'calls': []})   # a hub release without stats_7d
        v = m.sys_voice()
        ok(ran == ['status', 'stats'] and v['stats_src'] == 'local' and v['calls_7d'] == 9 and v['where'] == 'hub',
           f'sys_voice: hub without stats_7d falls back to the local log ({ran}, {v})')
        ran.clear()
        m.jcall = fake({'live': 1})   # a status without stats_7d or calls
        v = m.sys_voice()
        ok(v['stats_src'] == 'local' and v['calls_7d'] == 9 and v['where'] == 'hub' and v['live'] == 1,
           f'sys_voice: bare status, local log numbers ({v})')
        m.jcall = lambda cmd, timeout=None: None
        ok(m.sys_voice()['status'] == 'critical', 'sys_voice: nothing answers = critical')
    finally:
        m.jcall = saved


def test_vault_live():
    root = tempfile.mkdtemp(prefix='dash-test-vault-')
    try:
        make_fixture(root)
        fake = os.path.join(root, 'vault')
        with open(fake, 'w') as f:
            f.write(FAKE_VAULT)
        os.chmod(fake, 0o755)
        r = run_fixture(root, extra_env={'DASH_VAULT': fake, 'DASH_VAULT_TTL': '0'})
        ok(r.returncode == 0, f'vault fixture run exits 0 ({r.stderr[-200:]})')
        if r.returncode != 0:
            return
        L = json.loads(r.stdout)['dataflow']['live']
        ok(L['reads_24h'] == 5 and L['reads_24h_by'] == {'telegram': 1, 'mail': 1, 'documents': 1, 'finance': 1, 'search': 1},
           f"vault: 24 h lookups counted per source, status/admin/LLM events left out ({L['reads_24h']}, {L['reads_24h_by']})")
        ok(L['raw_released_7d'] == 1 and L['raw_tests_7d'] == 1 and L['raw_pending'] == 1, 'vault: raw releases split real/test, pending from status')
        ser = L['reads_daily']
        ok(len(ser) == 7 and ser[-1]['today'] and sum(x['n'] or 0 for x in ser) == 7,
           'vault: 7 daily columns ending today, every lookup of the week counted once')
        first = (datetime.now(ZoneInfo('Europe/Zurich')) - dt.timedelta(days=6, hours=12)).date().isoformat()
        full = [x['n'] for x in ser if not x['today'] and x['date'] > first]
        ok(all(x['n'] is None for x in ser if x['date'] < first) and L['reads_avg'] == (round(sum(full) / len(full), 1) if full else None),
           'vault: days before the first audit entry are empty; average over complete days only')
        ok(L['llm'] == 'Gemma 4 26B' and L['policy'] == 'v4.1' and L['llm_ok'] is True and L['errors_24h'] == 2 and 'calls_24h' not in L,
           'vault: model label, policy and errors from status/metrics (calls not shipped)')
        ok([(x['name'], x['ok']) for x in L['sources']] == [('Telegram', True), ('Gmail', True), ('paperless', True), ('Firefly', False)]
           and L['sources'][1]['note'] == '2 accounts', 'vault: sources and their state')
        ok('PLANTED' not in r.stdout and 'query' not in json.dumps(L) and 'reason' not in json.dumps(L), 'vault: audit text (query, reason) never copied')
        ok(os.path.exists(os.path.join(root, 'state', 'vault-live.json')), 'vault: numbers cached in the state dir')
        r2 = run_fixture(root, extra_env={'DASH_VAULT': '/bin/false', 'DASH_VAULT_TTL': '1800'})
        L2 = json.loads(r2.stdout)['dataflow']['live']
        ok(L2.get('reads_24h') == 5 and '_at' not in L2, 'vault: cache answers within the TTL without asking the vault')
        r3 = run_fixture(root, extra_env={'DASH_VAULT': '/bin/false', 'DASH_VAULT_TTL': '0'})
        ok(r3.returncode == 0 and json.loads(r3.stdout)['dataflow']['live'] == {'error': 'privacy vault not reachable'},
           'vault: unreachable vault gives an error note, the build still succeeds')
    finally:
        shutil.rmtree(root, ignore_errors=True)



# ------------------------------------------------------------------------------------------- 3. real run
def df_nodes(d):
    return (d.get('dataflow') or {}).get('nodes') or []


def test_real(tmpdir):
    out = os.path.join(tmpdir, 'data.json')
    # real local data, but never the real vault or the live Chrome (health() and the Data tab use these)
    r = subprocess.run([DASH, '--out', out], capture_output=True, text=True, timeout=180,
                       env=dict(os.environ, DASH_VAULT='/bin/false', DASH_BROWSER='/bin/false'))
    ok(r.returncode == 0, f'real run exits 0 ({r.stderr[-300:]})')
    if r.returncode != 0:
        return None
    d = json.load(open(out))
    for k in ('schema', 'generated_at', 'tokens', 'activity', 'health', 'dataflow', 'now'):
        ok(k in d, f'top-level key {k}')
    t = d['tokens']
    ok(t['totals']['tokens'] > 0, 'some tokens counted')
    ok(len(t['hourly']) == 48, '48 hourly buckets')
    dates = [x['date'] for x in t['daily']]
    ok(dates == sorted(dates) and len(set(dates)) == len(dates), 'daily sorted and unique')
    if len(dates) > 1:
        d0 = dt.date.fromisoformat(dates[0])
        ok(all(dt.date.fromisoformat(x) == d0 + dt.timedelta(days=i) for i, x in enumerate(dates)), 'daily contiguous')
    ok(abs(sum(b['cost_usd'] for b in t['by_kind'].values()) - t['totals']['cost_usd']) < 0.05, 'kinds add up to total cost')
    ok(sum(b['tokens'] for b in t['by_model']) == t['totals']['tokens'], 'models add up to total tokens')
    ok(sum(x['tokens']['main'] + x['tokens']['subagent'] + x['tokens']['headless'] for x in t['daily']) == t['totals']['tokens'],
       'daily adds up to total tokens')
    ok(all(abs(sum(x['cost_by_kind'].values()) - x['cost_usd']) < 0.01 for x in t['daily']), 'real daily cost_by_kind adds up')
    cp = t['cost_pace']
    ok(set(cp) >= {'days', 'avg_usd', 'today_usd', 'today_pct_of_avg', 'day_elapsed_pct'} and cp['days'] <= 14, 'cost pace fields')
    tl = d['activity']['timeline']
    starts = [x['start'] for x in tl['runs']]
    ok(starts == sorted(starts) and all(x['end'] >= tl['from'] for x in tl['runs']), 'real timeline sorted, last 24 h only')
    ok(all(set(o) == {'at', 'via'} and o['at'] >= tl['from'] for o in tl['owner_messages']), 'real owner messages: times only, last 24 h')
    ok('budget' not in d and 'assistant' not in d, 'retired blocks gone (budget, assistant -> codebase)')
    a0 = d['activity']
    ok(not ({'proactive', 'brain', 'capabilities', 'inbox_pending'} & set(a0)), 'activity ships no duplicates (proactive, brain, capabilities, inbox)')
    ok(not ({'tools_today', 'unknown_models', 'unknown_models_priced_as'} & set(t)) and 'agents_running' not in d['now'],
       'unused token/now fields gone')
    ok(not ({'container_uptime_s', 'disk_used_pct'} & set(d['health'])) and 'recurring' not in (a0['reminders'] or {}), 'unused health/reminder fields gone')
    tops = [x['tokens'] for x in t['top_runs']]
    ok(tops == sorted(tops, reverse=True), 'top runs sorted by tokens')
    ok('list price' in t['note'].lower() and 'estimate' in t['note'].lower(), 'cost labelled as an estimate')
    ok(all(c['status'] in ('ok', 'warn', 'critical', 'unknown') for c in d['health']['checks']), 'health statuses valid')
    ok(len(d['health']['checks']) >= 8, 'health has the expected checks')
    a = d['activity']
    ok(all({'when', 'job', 'schedule', 'enabled'} <= set(c) for c in a['cron']) and len(a['cron']) >= 10, 'cron parsed')
    ok(isinstance(a['todo']['waiting'], list), 'todo parsed')
    ok(a['reminders'] is None or set(a['reminders']) >= {'total', 'next_at', 'by_tag'}, 'reminders are counts only')
    ok(a['reminders'] is None or 'text' not in json.dumps(a['reminders']), 'no reminder text')
    cb = d.get('codebase') or {}
    st = cb.get('stats') or {}
    ok(set(cb) == {'stats', 'skills', 'agents', 'brain'} and set(st) == {'computed_local', 'plugins', 'hooks', 'tests'},
       f'codebase block: stats, one skills/agents list, brain ({sorted(cb)}, {sorted(st)})')
    ok(set(st.get('tests', {})) >= {'suites', 'checks'} and st['tests']['suites'] >= 5, 'codebase: test suites counted')
    ok(set(cb.get('brain') or {}) == {'commits', 'manual', 'last', 'last_7d'} and cb['brain']['commits'] >= cb['brain']['last_7d'] > 0,
       'codebase: one live config-snapshot counter')
    ok(len(cb.get('skills', [])) >= 5 and all(set(x) == {'name', 'what'} for x in cb['skills'] + cb['agents']), 'codebase: skills and subagents listed once')
    ok(os.path.exists(os.path.join(HOME, '.local', 'state', 'dash', 'assistant-stats.json')), 'codebase: hourly cache written')
    ok(not re.search(r'\.secrets|/context/|telegram\.md|signals\.md|/reports/', json.dumps(cb)), 'codebase: no private paths')
    g = d.get('growth') or {}
    gl_ = (g.get('days') or [{}])[-1]
    ok(g.get('days') and (not g.get('langs') or sum(x['loc'] for x in g['langs']) == gl_.get('loc_total')),
       'one line count: languages add up to the last growth point')
    gt = {x['date']: x for x in g.get('activity', [])}.get(t['today']['date'] if t.get('today') else '', {})
    tt = (t.get('today') or {})
    ok(not tt or (gt.get('subagents') == tt.get('subagent_runs', 0) and gt.get('owner_msgs') == tt['turns'].get('telegram', 0) + tt['turns'].get('you (app/terminal)', 0)),
       f"activity per day: today's point is this run's own counts ({gt} vs {tt.get('subagent_runs')})")
    ok({c['name'] for c in d['health']['checks']} >= {'Server (Unraid)', 'Array capacity'} or not os.path.exists(os.path.join(HOME, '.local', 'state', 'context', 'today.json')),
       'health includes the server checks')
    ok(all(set(n) <= {'id', 'label', 'sub', 'group'} for n in df_nodes(d)) and not ({'routes', 'edge_labels', 'viewbox', 'version', 'sensitivity_levels'} & set(d['dataflow'])),
       'data map drawing fields gone (Architecture draws the system)')
    df = d['dataflow']
    ids = {n['id'] for n in df.get('nodes', [])}
    ok(all(f['from'] in ids and f['to'] in ids and (not f.get('via') or f['via'] in ids) for f in df.get('flows', [])),
       'every flow references known nodes')
    ok(all(f['filter'] in ('raw', 'anonymized', 'metadata') for f in df['flows']), 'flow filters valid')
    ok(all(s_['sensitivity'] in ('low', 'medium', 'high', 'critical') for s_ in df['stores']), 'store sensitivity valid')
    ok(any(f.get('highlight') and f['to'] == 'anthropic' for f in df['flows']), 'Anthropic flow highlighted')
    ok(any(f['from'] == 'vault' and f['filter'] == 'anonymized' for f in df['flows']), 'vault path shown as anonymized')
    gl = d.get('goals') or {}
    ok(isinstance(gl.get('goals'), list) and gl['total'] == sum(g['total'] for g in gl['goals']), 'real goals parsed and add up')
    ok(len(gl['goals']) >= 1 and all(g['title'] for g in gl['goals']), 'GOALS.md has titled goals')
    dm = gl.get('done_metrics') or {}
    ok(all(isinstance((dm.get(k) or {}).get('d7'), int) and dm[k]['d7'] <= dm[k]['d30'] for k in ('goal_steps', 'todo_done', 'built', 'answered'))
       and isinstance(dm.get('waiting_now'), int) and dm['waiting_now'] == len(a['todo']['waiting'])
       and sum(g['ticked_30d'] for g in gl['goals']) == dm['goal_steps']['d30'], f'real done metrics: 7 d <= 30 d, waiting = the TODO card, per-goal ticks add up: {dm}')
    ln = a.get('lastnight') or {}
    ok(set(ln) == {'window', 'tonight', 'earlier', 'untimed'} and len(ln.get('earlier', [])) == 7, 'real lastnight: tonight + 7 nights')
    nts = [ln.get('tonight') or {}] + ln.get('earlier', [])
    ok(all(n.get('count') == len(n.get('items', [])) or n.get('count', 0) > 40 for n in nts), 'real lastnight: counts match')
    ok(all(len(e['text']) <= 90 and re.fullmatch(r'\d\d:\d\d', e['time']) and (e['time'] < '08:00' or e['time'] >= '22:00')
           for n in nts for e in n.get('items', [])), 'real lastnight: timed night items, at most 90 chars')
    dl = a['dayline']
    ok(dl is None or (set(dl) == {'date', 'today', 'done', 'changes'} and dl['today'] and isinstance(dl['changes'], int)), 'real dayline is today\'s')
    cx = d.get('context')
    ok(cx is None or set(cx) == {'generated_local', 'age_min', 'hn'}, 'real context card has only the HN picks')
    sec = [s_ for s_ in df['stores'] if s_['id'] == 'secrets'][0]
    ok({'homeassistant', 'github', 'gmail', 'firefly'} <= ids and any(f['from'] == 'homeassistant' for f in df['flows']),
       'map shows Home Assistant (location), GitHub and the vault-only sources')
    ok(not any(n['id'] == 'google' and 'Gmail' in n['label'] for n in df['nodes']), 'Gmail is no longer drawn as a claude.ai connector')
    live = df.get('live') or {}

    def leaves(o):
        if isinstance(o, dict):
            for v_ in o.values():
                yield from leaves(v_)
        elif isinstance(o, list):
            for v_ in o:
                yield from leaves(v_)
        else:
            yield o
    ok('error' in live or (isinstance(live.get('reads_24h'), int) and len(live.get('reads_daily', [])) == 7),
       'real vault numbers present (or an error note)')
    ok(all(v_ is None or isinstance(v_, (int, float, bool)) or (isinstance(v_, str) and len(v_) <= 40) for v_ in leaves(live))
       and not re.search(r'"(query|reason|text|chat|sender)"', json.dumps(live)), 'vault numbers are counts and short labels only')
    ok('bytes' not in sec.get('live', {}), 'secrets store shows a file count only')
    return d


# ------------------------------------------------------------------------------------------- 4. privacy sweep
def private_literals():
    """Values that must never show up: owner chat ids and streets from the memory notes (never printed)."""
    lits = set()
    try:
        acc = json.load(open(os.path.join(HOME, '.claude', 'channels', 'telegram', 'access.json')))
        lits |= {str(x) for x in acc.get('allowFrom', [])}
    except (OSError, ValueError):
        pass
    rx = re.compile(r"\b([A-ZÄÖÜ][\wäöüß-]+(?:strasse|straße|gasse|weg)\s*\d+\w?)")
    for f in glob.glob(os.path.join(HOME, '.claude', 'projects', '-home-node-workspace', 'memory', '*.md')):
        lits |= set(rx.findall(open(f, encoding='utf-8', errors='replace').read()))
    return {x for x in lits if len(x) >= 5}


def luhn(num):
    ds = [int(c) for c in num][::-1]
    tot = sum(ds[0::2]) + sum(sum(divmod(2 * x, 10)) for x in ds[1::2])
    return tot % 10 == 0


def heat_rows(data):
    """The cron heatmap's `ok` rows (systems.cron.heat.ok = [[job, runs per hour]]): the runs are one base-36 digit per
    hour of `hours`, so a quiet job reads as a 24-digit string. Only rows of exactly that shape count; anything else
    there is swept like any other string."""
    hm = (((data or {}).get('systems') or {}).get('cron') or {}).get('heat') or {}
    n = len(hm.get('hours') or []) if isinstance(hm, dict) else 0
    rows = hm.get('ok') if isinstance(hm, dict) else None
    return {r[1] for r in rows or [] if n and isinstance(r, list) and len(r) == 2 and isinstance(r[1], str)
            and re.fullmatch(r'[0-9a-z]{%d}' % n, r[1])}


def sweep(name, text, strs, encoded=()):
    """encoded: strings that are counts in a known encoding (heat_rows), left out of the long-number check only."""
    r = subprocess.run([os.path.join(HOME, 'bin', 'secretscan'), '--stdin', '-q'], input=text, capture_output=True, text=True)
    ok(r.returncode == 0, f'{name}: secretscan clean ({r.stdout.strip()[:200]})')
    joined = '\n'.join(strs)
    checks = {
        'e-mail': r'[\w.+-]+@[\w-]+\.[\w.-]+',
        'phone': r'(?<![\w.])\+?\d{2,3}[\s/.-]?\(?\d{2,3}\)?[\s/.-]?\d{3}[\s/.-]?\d{2}[\s/.-]?\d{2}(?![\w.])',
        'IPv4': r'\b(?:\d{1,3}\.){3}\d{1,3}\b',
        'IBAN': r'\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}',
        'AHV': r'\b756[.\s]?\d{4}[.\s]?\d{4}[.\s]?\d{2}\b',
        'bot token': r'\b\d{8,10}:[A-Za-z0-9_-]{30,}',
        'api key': r'sk-ant-[A-Za-z0-9_-]{10,}',
        'long number in text': r'(?<![\w.:/-])\d{7,}(?![\w.-])',
        'street + number': r"\b[A-ZÄÖÜ][\wäöüß-]+(?:strasse|straße|gasse)\s*\d+",
        'url with query': r'https?://\S+\?\S+',
    }
    for label, rx in checks.items():
        hits = re.findall(rx, '\n'.join(s for s in strs if s not in encoded) if label == 'long number in text' else joined)
        ok(not hits, f'{name}: no {label} ({len(hits)} hit(s))')
    cards = [m for m in re.findall(r'(?<!\d)(?:\d[ -]?){13,19}(?!\d)', joined) if luhn(re.sub(r'\D', '', m))]
    ok(not cards, f'{name}: no card-like numbers ({len(cards)})')
    for lit in private_literals():
        ok(lit not in text, f'{name}: a private literal from access.json/memory is absent')


def log_lines(data):
    a = data.get('activity') or {}
    ln = a.get('lastnight') or {}
    dl = a.get('dayline') or {}
    out = [e['text'] for n in [ln.get('tonight') or {}] + ln.get('earlier', []) for e in n.get('items', [])]
    out += [e['text'] for e in a.get('changelog', []) + dl.get('done', [])]
    return out


def test_log_privacy(m, data):
    """Names and amounts in log-derived lines: no surname from the people note, a first name only if the rest of the page
    shows it, no money amounts."""
    lines = log_lines(data)
    ok(len(lines) > 10, f'log-derived lines found ({len(lines)})')
    m._PEOPLE = None
    first, sur = m.people()
    shown = m.shown_words(data, m.LOG_BLOCKS)
    bad = {w for w in (first - shown) | sur if any(re.search(r'\b' + re.escape(w) + r'\b', x) for x in lines)}
    ok(not bad, f'log lines: no names from the people note ({len(bad)} hit(s))')
    money = [x for x in lines if re.search(r"(?:CHF|Fr\.|EUR|USD|\$|€|£)\s?~?\d|\d\s?(?:CHF|Fr\.|EUR|€|£)(?![A-Za-z])", x)]
    ok(not money, f'log lines: no currency amounts ({len(money)} hit(s))')


def test_privacy(d):
    data_text = open(os.path.join(HOME, '.local', 'state', 'dash', 'data.json')).read()
    data = json.loads(data_text)
    probe = {'systems': {'cron': {'heat': {'hours': list(range(24)), 'ok': [['a', '0' * 23 + '1'], ['b', '0791234567'], ['c', '0' * 25]]}}},
             'x': '0' * 23 + '1'}
    ok(heat_rows(probe) == {'0' * 23 + '1'} and heat_rows({'systems': {'cron': {'heat': None}}}) == set(),
       'privacy sweep: only well-formed heatmap rows are exempt from the long-number check')
    sweep('data.json', data_text, list(strings(data)), heat_rows(data))
    for p in (TEMPLATE, PAGE, FRAG):
        if os.path.exists(p):
            txt = open(p).read()
            m = re.search(r'<script id="dash-data"[^>]*>(.*?)</script>', txt, re.S)
            emb = json.loads(m.group(1).replace('<\\/', '</')) if m else None
            sweep(os.path.basename(p), txt, list(strings(emb)) if emb else [], heat_rows(emb) if isinstance(emb, dict) else ())


# ------------------------------------------------------------------------------------------- 5. page structure
def structure(page, name):
    ok('<title>Claude Bot Dashboard</title>' in page[:8192], f'{name}: title in the first 8 KB')
    ok('<!--dash:head-->' in page and '<!--dash:body-->' in page, f'{name}: artifact markers present')
    ok(not re.search(r'<script[^>]+src=|<link[^>]+href=(?!"data:)|@import|url\(\s*["\']?https?:', page), f'{name}: no external scripts, styles or fonts (the icon is a data: URI)')
    ok(not re.search(r'fetch\(\s*["\']https?:', page), f'{name}: no fetch to other hosts')
    ok('prefers-color-scheme: dark' in page and ':root[data-theme="dark"]' in page, f'{name}: dark mode tokens')
    ok('[hidden] { display: none !important; }' in page, f'{name}: hidden attribute enforced')


def test_template(m):
    """The template holds no data (data block {}; dash-data fills a copy) and shows profile values only as {{key}}
    tokens from dash-data's allowlist. Runs in a checkout too (the repo's dash/index.html)."""
    ok(os.path.exists(TEMPLATE), 'page template exists')
    if not os.path.exists(TEMPLATE):
        return
    page = open(TEMPLATE, encoding='utf-8').read()
    d = re.search(r'<script id="dash-data" type="application/json" data-mode="fallback">(.*?)</script>', page, re.S)
    ok(bool(d) and d.group(1) == '{}', 'template: data block present and empty ({})')
    toks = set(re.findall(r'\{\{([a-z_]+(?:\.[a-z_]+)+)\}\}', page))
    ok(toks and toks <= set(m.PAGE_PROFILE_KEYS), f'template: profile tokens only from the allowlist ({sorted(toks)})')
    structure(page, 'template')
    test_arch_static(page)
    test_live_wiring(page)
    test_beacon(page)
    test_reload(page)
    ok(re.search(r'<meta name="dash-version" content="">', page) is not None, 'template: dash-version meta empty (dash-data stamps a copy)')
    test_hub_slo_static(page)
    test_ci_static(page)
    test_doing_static(page)
    test_fixed_scope(page)


BEACON_RUN = r'''
var src = require('fs').readFileSync(0, 'utf8');
var t = 0, hid = false, sent = [];
var beaconer = new Function(src + '; return beaconer;')();
var b = beaconer({ now: function () { return t; }, hidden: function () { return hid; },
                   send: function (v, why) { sent.push([t / 60000, v, why]); } });
var M = 60000;
b.load('system');                          // first visible load
t = 5 * M; b.tab('system');                // the page's own tab again within 10 min: nothing
t = 6 * M; b.tab('usage');                 // he picks another tab
t = 7 * M; b.hide(); t = 17 * M; b.show('usage');            // away 10 min: same session
t = 20 * M; b.hide(); b.hide(); t = 60 * M; b.show('usage'); b.show('usage');   // blur + hidden, 40 min: one focus
t = 61 * M; hid = true; b.hide(); b.tab('today');            // a hidden page never beacons
t = 200 * M; hid = false; b.show('today');                   // back after 139 min: focus
var c = beaconer({ now: function () { return t; }, hidden: function () { return hid; },
                   send: function (v, why) { sent.push([t / 60000, v, 'c:' + why]); } });
t = 300 * M; hid = true; c.load('system');                   // loaded in a background tab: waits
t = 310 * M; hid = false; c.show('system');                  // shown: that is the load
process.stdout.write(JSON.stringify(sent));
'''


def test_beacon(page):
    """dash.view beacon: one per real open (first visible load, a tab he picks, back in view after > 30 min away);
    the 5-min data refresh re-renders without a beacon. Behaviour runs the page's own beaconer() in node."""
    ok("if (!auto) beacon.tab(view);" in page and "go(current, true, true, true);" in page
       and "if (!booted) { booted = true; beacon.load(current); }" in page and not re.search(r'\bseen\(', page),
       'beacon: the refresh re-render (go auto) sends nothing; the first render sends one load')
    ok("fetch('live/seen?v=' + encodeURIComponent(view) + '&why=' + why" in page,
       'beacon: same-origin GET with the tab and why only')
    ok("addEventListener('visibilitychange', function () { if (document.hidden) beacon.hide(); else beacon.show(current); })" in page
       and "addEventListener('blur', function () { beacon.hide(); })" in page, 'beacon: wired to visibility and window focus')
    m = re.search(r'/\* beacon:start.*?\*/(.*?)/\* beacon:end \*/', page, re.S)
    ok(bool(m), 'beacon: beaconer() between its markers')
    if not m or not shutil.which('node'):
        print('skip: beacon behaviour (no node)' if m else '')
        return
    r = subprocess.run(['node', '-e', BEACON_RUN], input=m.group(1), capture_output=True, text=True, timeout=30)
    got = json.loads(r.stdout or '[]')
    want = [[0, 'system', 'load'], [6, 'usage', 'tab'], [60, 'usage', 'focus'], [200, 'today', 'focus'],
            [310, 'system', 'c:load']]
    ok(r.returncode == 0 and got == want, f'beacon: load, picked tab, focus after > 30 min away only ({got} {r.stderr[:200]})')


RELOAD_RUN = r'''
var src = require('fs').readFileSync(0, 'utf8');
var pageReloader = new Function(src + '; return pageReloader;')();
var t = 0, busy = false, log = [], mem = {};
var store = { get: function (k) { return k in mem ? mem[k] : null; }, set: function (k, v) { mem[k] = String(v); } };
function tab(own, st) {   // one open tab (a page load) on version `own`
  return pageReloader({ own: own, now: function () { return t; }, busy: function () { return busy; }, store: st || store,
                        save: function () { log.push('save:' + own); }, reload: function () { log.push('reload:' + own + '@' + t / 60000); } });
}
var M = 60000, out = {};
var a = tab('vA');
out.same = a.check('vA');                      // data.json from the same deploy: nothing
out.none = [a.check(undefined), a.check(''), a.check(42)];   // an older dash-data without the field, or junk: nothing
out.noOwn = tab('').check('vB');               // template opened as a file / inline copy: never reloads
t = 1 * M; busy = true;
out.busy = a.check('vB');                      // a deploy while he types or just touched the page: put off
out.focusBusy = a.focus();                     // back in view, still busy: still put off
busy = false; t = 2 * M;
out.focus = a.focus();                         // next focus, idle: reload (state saved first)
var b = tab('vA');                             // the reload got the old copy (publish race): no loop
t = 3 * M; out.again = b.check('vB');
t = 8 * M; out.retry = b.check('vB');          // > 5 min later: one retry
var c = tab('vA');
t = 30 * M; out.third = c.check('vB');         // never a third time for that version
out.focusAfter = c.focus();
var d = tab('vB');                             // the new copy: versions match, quiet
t = 31 * M; out.fresh = d.check('vB');
t = 40 * M; out.next = d.check('vC');          // the next deploy: one reload again
var broken = { get: function () { throw new Error('blocked'); }, set: function () { throw new Error('blocked'); } };
out.noStore = tab('vA', broken).check('vB');   // storage blocked: no guard, so no reload
var lies = { get: function () { return null; }, set: function () {} };
out.noKeep = tab('vA', lies).check('vB');      // storage that keeps nothing: no reload
out.log = log;
process.stdout.write(JSON.stringify(out));
'''


def test_reload(page):
    """New page code reaches an open tab: dash-data stamps the template hash into <meta name="dash-version"> and
    data.json (page_version); each data refresh compares them and reloads once per version (one retry after 5 min when
    the reload still got the old copy), never while he types or just touched the page (then on the next focus), never
    without a working sessionStorage guard; the tab, scroll and folds come back from sessionStorage. The decision runs
    the page's own pageReloader() in node."""
    ok('<meta name="dash-version" content="' in page[:4096], 'reload: the dash-version meta is in the head')
    ok("if (reloader.check(d && d.page_version) === 'reload') return;" in page
       and "if (document.hidden || reloader.focus() === 'reload') return;" in page,
       'reload: every data refresh checks the version; a put-off reload runs on the next focus')
    ok("own: (document.querySelector('meta[name=\"dash-version\"]') || {}).content || ''" in page
       and 'reload: function () { location.reload(); }' in page, 'reload: own version from the meta, location.reload')
    ok("sessionStorage.setItem(RESTORE_KEY, JSON.stringify({ view: current, y: window.scrollY, folds: folds" in page
       and "if (!again && restore) {" in page and 'initial = viewOf(restore.view);' in page,
       'reload: tab, scroll position and folds saved before and restored by the first render')
    ok('/^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName) || a.isContentEditable' in page and 'Date.now() - lastTouch < BUSY_MS' in page,
       'reload: busy = a focused field, a text selection or a touch in the last 30 s')
    m = re.search(r'/\* reload:start \*/(.*?)/\* reload:end \*/', page, re.S)
    ok(bool(m), 'reload: pageReloader() between its markers')
    if not m or not shutil.which('node'):
        print('skip: reload behaviour (no node)' if m else '')
        return
    r = subprocess.run(['node', '-e', RELOAD_RUN], input=m.group(1), capture_output=True, text=True, timeout=30)
    got = json.loads(r.stdout or '{}')
    want = {'same': False, 'none': [False, False, False], 'noOwn': False, 'busy': 'later', 'focusBusy': 'later',
            'focus': 'reload', 'again': False, 'retry': 'reload', 'third': False, 'focusAfter': False, 'fresh': False,
            'next': 'reload', 'noStore': False, 'noKeep': False,
            'log': ['save:vA', 'reload:vA@2', 'save:vA', 'reload:vA@8', 'save:vB', 'reload:vB@40']}
    ok(r.returncode == 0 and got == want, f'reload: once per version, put off while busy, one retry, no loop ({got} {r.stderr[:300]})')


def test_page():
    page = open(PAGE).read()
    structure(page, 'page')
    ok(not re.search(r'\{\{[a-z_.]+\}\}', page), 'page: every profile token filled')
    m = re.search(r'<script id="dash-data" type="application/json" data-mode="fallback">(.*?)</script>', page, re.S)
    ok(bool(m), 'index.html carries the fallback data block')
    if m:
        emb = json.loads(m.group(1).replace('<\\/', '</'))
        cur = json.load(open(os.path.join(HOME, '.local', 'state', 'dash', 'data.json')))
        ok(emb.get('generated_at') == cur.get('generated_at'), 'inlined copy matches data.json')
        mv = re.search(r'<meta name="dash-version" content="([^"]*)">', page)
        want = 'v' + hashlib.sha256(open(TEMPLATE, encoding='utf-8').read().encode()).hexdigest()[:12]
        ok(mv and mv.group(1) == cur.get('page_version') == emb.get('page_version') == want,
           f'page version: the page, its inlined copy and data.json carry the template hash ({mv.group(1) if mv else None}, {cur.get("page_version")}, {want})')
    ok(os.path.exists(FRAG), 'dashboard.html exists')
    if os.path.exists(FRAG):
        frag = open(FRAG).read()
        ok(not re.search(r'<!doctype|<html|<head[\s>]|<body', frag, re.I), 'artifact fragment has no doctype/html/head/body')
        ok(frag.lstrip().startswith('<title>Claude Bot Dashboard</title>'), 'fragment starts with its title')
        ok('data-mode="inline"' in frag, 'fragment uses the inline data only')
        ok(len(frag.encode()) < 16 * 1024 * 1024, 'fragment under 16 MB')


ARCH_WORDS = ['Main session', 'Policy filter', 'Anthropic', 'Cron jobs', 'Vault bot', 'nginx VM', 'Unraid server']


def test_agents_tab(page, data):
    """Agents tab: live list over flowd /live/agents, a session drawer over /live/agent?id=, both opened only on
    the tab; every text from the bot (already agentfeed.clean'ed) drawn as text, never as HTML; the data block holds only
    filtered fields (no command lines, paths, prompts or results)."""
    ok("var VIEWS = ['health', 'ci', 'agents', 'today'" in page and 'id="tab-agents"' in page and 'agents: renderAgents' in page,
       'agents: a tab, a section and a renderer')
    ok("new EventSource('live/agents')" in page and "new EventSource('live/agent?id=' + encodeURIComponent(id))" in page
       and "agLive(view === 'agents');" in page, 'agents: live list + session streams, opened from the tab only')
    m = re.search(r'// -{10,} agents: who is working on what.*?// -{10,} architecture', page, re.S)
    js = m.group(0) if m else ''
    ok(js and 'innerHTML' not in js and 'insertAdjacentHTML' not in js, 'agents: no HTML from data (textContent only)')
    ok("/^a[0-9a-f]{6,24}$/.test(id)" in js, 'agents: only well-formed agent ids are requested')
    A = (data or {}).get('agents')
    if A is None:
        print('skip: agents data block (agentfeed or agent-watch --json missing)')
        return
    ok(set(A) == {'now', 'hours', 'main', 'agents'}, f'agents: block shape ({sorted(A)})')
    keys = {'id', 'title', 'status', 'start', 'last', 'runtime_s', 'model', 'effort', 'type', 'depth', 'parent', 'tools',
            'errors', 'tokens', 'cost', 'trigger', 'now', 'result_len'}
    ok(all(set(a) == keys for a in A['agents']), 'agents: rows carry only the filtered fields')
    blob = json.dumps(A)
    ok(not re.search(r'/home/|~/|https?://|```|\.secrets', blob), 'agents: no paths, links or code in the block')
    ok(all(re.fullmatch(r'a[0-9a-f]{6,24}', a['id']) for a in A['agents']), 'agents: ids only agent ids')


def test_live_wiring(page):
    """Live data-flow panel (flowd /live): mapped lines are drawn in both maps, flowd edge ids are real, same origin only."""
    m = re.search(r'var LIVE_MAP = \{(.*?)\};', page, re.S)
    n = re.search(r'var LIVE_NONE = \{(.*?)\};', page, re.S)
    t = re.search(r'<template id="arch-tpl">(.*?)</template>', page, re.S)
    ok(bool(m and n and t), 'live: LIVE_MAP, LIVE_NONE and the map template present')
    if not (m and n and t):
        return
    pairs = re.findall(r"'([\w:-]+)': \[([^\]]*)\]", m.group(1))
    keys = {k for k, _ in pairs}
    vals = {v for _, vs in pairs for v in re.findall(r"'([^']+)'", vs)}
    maps = re.findall(r'<svg class="arch (arch-[pl])"(.*?)</svg>', t.group(1), re.S)
    for name, body in maps:
        body = re.sub(r'<!--.*?-->', '', body, flags=re.S)   # comments name the id scheme
        drawn = set(re.findall(r'data-edge="([\w-]+)"', body))
        ok(all(k in drawn for k in keys if k != 'node:main') and 'data-node="main"' in body, f'live: every mapped line is drawn in {name}')
        none = set(re.findall(r"'([\w-]+)':", n.group(1)))
        inside = drawn - keys - none
        ok(inside <= {'feed-filter', 'filter-approvals', 'filter-llm', 'data-llm', 'paperless-unraid'},
           f'live: {name}: only lines inside the vault have no feed ({sorted(inside)})')
    fl = os.path.join(HOME, 'bin', 'flowd')
    edges = subprocess.run([fl, 'edges'], capture_output=True, text=True).stdout.split() if os.path.exists(fl) else []
    ok(bool(edges) and vals <= set(edges) and set(edges) <= vals, 'live: map uses exactly the flowd edge ids')
    ok("new EventSource('live?" in page and not re.search(r"EventSource\(\s*['\"](?:https?:)?//", page)
       and "fetch('live/more?id='" in page, 'live: same-origin stream and more-fetch only')
    ok('liveWire(v);' in page and 'livePulse(view ===' in page and "if (view !== 'architecture') liveClose();" in page,
       'live: wired on render, pulse stream only while the tab is shown')
    lines = [ln for ln in page.split('\n') if 'localStorage.' in ln]
    ok(lines and all('try {' in ln for ln in lines), 'live filters: every localStorage access sits in a try')
    ok("'dash-live-f:' + key" in page and "'dash-live-f:' + live.key" in page, 'live filters: stored per view')
    ok('innerHTML' not in page.split('var LF_KEYS')[1].split('function liveFlash')[0], 'live filters: highlight without innerHTML')
    ok("['kind', 'from', 'to', 'dir'].forEach" in page, 'live filters: kind, from, to and direction go to flowd')
    ok('carry metadata only' not in page and "'me→vault': 'Everything my session sees" in page,
       'live: the vault note says it shows what my session sees')
    ok("q.push('err=1')" in page and "'&since='" in page and "'edge=' + encodeURIComponent(edges.join(','))" in page,
       'live filters: errors and time window go to flowd, the search stays in the page')


def test_arch_static(page):
    ok('id="tab-architecture"' in page and "'privacy', 'architecture']" in page and 'architecture: renderArchitecture' in page,
       'architecture: tab, view list and renderer wired')
    m = re.search(r'<template id="arch-tpl">(.*?)</template>', page, re.S)
    ok(bool(m), 'architecture: template present')
    if not m:
        return
    t = m.group(1)
    svgs = {c: (a, b) for c, a, b in re.findall(r'<svg class="arch (arch-[pl])"([^>]*)>(.*?)</svg>', t, re.S)}
    ok(set(svgs) == {'arch-p', 'arch-l'} and t.count('<svg class="arch') == 2, 'architecture: portrait and landscape SVG, one each')
    for c, (a, b) in svgs.items():
        ok('role="img"' in a and re.search(r'aria-label="[^"]{80,}"', a), f'architecture {c}: inline SVG with role=img and a real aria-label')
        ok(all(w in b for w in ARCH_WORDS), f'architecture {c}: all zones and key parts drawn')
        refs, defs = set(re.findall(r'url\(#([\w-]+)\)', b)), set(re.findall(r'\bid="([^"]+)"', b))
        ok(refs and refs <= defs, f'architecture {c}: uses only its own markers (the hidden map cannot break it)')
    if set(svgs) == {'arch-p', 'arch-l'}:
        test_arch_pair(svgs)
    ok('<figcaption' in t and 'privacy boundary' in t and 'control or approval' in t, 'architecture: caption and legend')
    ok(not re.search(r'<script|<style|<foreignObject|<image|href="http', t), 'architecture: SVG self-contained (no script/style/foreignObject/external refs)')
    ok(not re.search(r'#[0-9a-fA-F]{3,6}\b', t), 'architecture: colours only via CSS variables')
    ok(not re.search(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', t), 'architecture: no IP addresses')
    ok(not re.search(r'[\w.+-]+@[\w-]+\.[\w.]+|https?://', t), 'architecture: no e-mails or URLs')
    r = subprocess.run([os.path.join(HOME, 'bin', 'secretscan'), '--stdin'], input=t, capture_output=True, text=True,
                       timeout=60)   # ~/bin by path: CI's PATH has no ~/bin
    ok(r.returncode == 0, 'architecture: secretscan clean')
    ids = re.findall(r'\bid="([^"]+)"', t)
    ok(len(ids) == len(set(ids)) and all(i.startswith('ax-') for i in ids), 'architecture: SVG ids unique and prefixed ax-')
    ok(all(re.search(r'id="' + i + r'"', t) for i in re.findall(r'url\(#([\w-]+)\)', t)), 'architecture: every marker reference resolves')
    m = load_module()
    first, sur = m.people()
    txt = ' '.join(re.findall(r'>([^<]+)<', t) + re.findall(r'aria-label="([^"]+)"', t))
    if not os.path.exists(m.PEOPLE_NOTE):   # checkout/CI: no people note, nothing to leak
        print('skip: architecture people-name check (no people note)')
        return
    ok(first and not any(re.search(r'\b' + re.escape(w) + r'\b', txt) for w in first | sur), 'architecture: no names from the people note')


def arch_parts(body):
    """{'node': {name: rect classes}, 'edge': {name: path classes}} of one map, plus a list of duplicate names."""
    root = ET.fromstring('<svg>' + body + '</svg>')
    out, dup = {'node': {}, 'edge': {}}, []
    for g in root.iter('g'):
        for kind in ('node', 'edge'):
            name = g.get('data-' + kind)
            if name is None:
                continue
            if name in out[kind]:
                dup.append(name)
            tag = 'rect' if kind == 'node' else 'path'
            el = next((x for x in g if x.tag == tag), None)
            out[kind][name] = el.get('class') if el is not None else None
    return out, dup


def test_arch_pair(svgs):
    """Portrait (phones) and landscape (>= 760 px): same nodes and edges by name, same edge styles."""
    vb = {c: [float(x) for x in re.search(r'viewBox="([^"]+)"', a).group(1).split()] for c, (a, b) in svgs.items()}
    ok(vb['arch-p'][:3] == [0, 0, 360] and 836 <= vb['arch-p'][3] <= 1140, 'architecture: phone map keeps its 360-wide portrait viewBox (height 836-1140; 1134 since the live views row, 30.10)')
    ok(vb['arch-l'][2] >= 1000 and vb['arch-l'][2] >= 1.6 * vb['arch-l'][3], 'architecture: landscape map is wide (>= 1000 units, >= 1.6:1)')
    try:
        (p, dp), (l, dl) = arch_parts(svgs['arch-p'][1]), arch_parts(svgs['arch-l'][1])
    except ET.ParseError as e:
        ok(False, f'architecture: both maps parse as XML ({e})')
        return
    ok(not dp and not dl, f'architecture: node and edge names unique within each map ({dp + dl})')
    ok(len(p['node']) >= 20 and set(p['node']) == set(l['node']), 'architecture: same data-node names in both maps')
    ok(len(p['edge']) >= 20 and set(p['edge']) == set(l['edge']), 'architecture: same data-edge names in both maps')
    ok(all(re.fullmatch(r'[a-z]+', n) for n in p['node']), 'architecture: node names are plain lowercase words')
    ok(all(len(e.split('-')) == 2 and set(e.split('-')) <= set(p['node']) for e in p['edge']), 'architecture: edge names are from-to node names')
    ok(all(v and 'b' in v.split() or v == 'grp' for v in list(p['node'].values()) + list(l['node'].values())), 'architecture: every node has its box')
    ok(all(p['edge'][e] and p['edge'][e].split()[0] == 'e' for e in p['edge']) and all(p['edge'][e] == l['edge'].get(e) for e in p['edge']),
       'architecture: every edge has a path, styled the same in both maps')
    ok(sum('hot' in (v or '') for v in l['edge'].values()) == 1 and sum('ok' in (v or '').split() for v in l['edge'].values()) >= 1,
       'architecture: one Anthropic edge, answers-only edge present')
    ok(svgs['arch-l'][1].count('data-part="boundary"') == 2 and svgs['arch-p'][1].count('data-part="boundary"') == 2, 'architecture: boundary line and lock tagged in both maps')


# ------------------------------------------------------------------------------------------- 7. dash-growth
def test_growth():
    gb = os.environ.get('GROWTH_BIN', os.path.join(HOME, 'bin', 'dash-growth'))
    root = tempfile.mkdtemp(prefix='dash-growth-')
    try:
        tree, vwt = os.path.join(root, 'home'), os.path.join(root, 'vault')
        today = dt.datetime.now(ZoneInfo('Europe/Zurich')).date()
        d1, d2, d3 = (today - dt.timedelta(days=k) for k in (3, 2, 1))

        def w(base, rel, text, mode=0o644):
            p = os.path.join(base, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'w') as f:
                f.write(text)
            os.chmod(p, mode)

        def commit(gd, wt, day, hhmm):
            stamp = f'{day.isoformat()}T{hhmm}:00+02:00'
            env = dict(os.environ, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp, GIT_AUTHOR_NAME='t', GIT_COMMITTER_NAME='t',
                       GIT_AUTHOR_EMAIL='t@example.invalid', GIT_COMMITTER_EMAIL='t@example.invalid')
            g = ['git', '--git-dir', gd, '--work-tree', wt]
            subprocess.run(g + ['add', '-A'], env=env, check=True, capture_output=True, cwd=wt)
            subprocess.run(g + ['commit', '-q', '-m', 'x'], env=env, check=True, capture_output=True, cwd=wt)
        brain = os.path.join(root, 'brain.git')
        subprocess.run(['git', 'init', '-q', '--bare', brain], check=True)
        subprocess.run(['git', 'init', '-q', vwt], check=True)
        w(tree, 'bin/a', '#!/usr/bin/env python3\nx = 1\n\ny = 2\n', 0o755)
        w(tree, 'workspace/CLAUDE.md', 'x' * 2047 + '\n')
        w(tree, 'crontab', '# c\nPATH=/bin\n* * * * * a\n5 * * * * b\n')
        w(tree, '.claude/skills/s1/SKILL.md', 'a\nb\nc\nd\n')
        w(tree, '.claude/agents/x.md', 'a\nb\n')
        commit(brain, tree, d1, '10:00')
        w(tree, 'bin/a', '#!/usr/bin/env python3\nx = 1\ny = 2\nz = 3\nw = 4\n', 0o755)   # 5 lines: the day's last commit wins
        commit(brain, tree, d1, '22:00')
        w(tree, 'bin/b', '#!/bin/sh\necho hi\n', 0o755)
        w(tree, 'opt/claude-vault/app/v.py', ''.join(f'v{i} = {i}\n' for i in range(6)))
        w(tree, 'opt/claude-vault/data/x.json', '{"k": "PRIVATE-NEVER-COUNTED"}\n' * 50)
        w(tree, 'opt/claude-vault/app/notes.txt', 'not code\n' * 9)
        commit(brain, tree, d3, '12:00')
        w(vwt, 'app/v.py', ''.join(f'v{i} = {i}\n' for i in range(6)))
        w(vwt, 'app/w.py', 'w = 1\n')
        w(vwt, 'data/big.json', '{}\n' * 99)
        commit(os.path.join(vwt, '.git'), vwt, d3, '12:30')
        w(tree, 'opt/claude-vault/app/w.py', 'w = 1\n')            # live tree = vault repo state
        dd = os.path.join(root, 'data.json')
        with open(dd, 'w') as f:
            json.dump({'tokens': {'daily': [{'date': d1.isoformat(), 'turns': {'heartbeat': 4, 'telegram': 2, 'you (app/terminal)': 1},
                                             'subagent_runs': 3}]}}, f)
        out = os.path.join(root, 'growth.json')
        # every DASH_GROWTH_* path points into the fixture: the monorepo too (no repo there), else dash-growth adds the
        # real ~/opt/assistant history to the fixture's days
        env = {k: v for k, v in os.environ.items() if not k.startswith('DASH_GROWTH_')}
        env.update(DASH_GROWTH_OUT=out, DASH_GROWTH_BRAIN=brain, DASH_GROWTH_VAULT=vwt, DASH_GROWTH_TREE=tree,
                   DASH_GROWTH_DASHDATA=dd, DASH_GROWTH_MONO=os.path.join(root, 'no-mono'))
        r = subprocess.run([gb, '--backfill'], env=env, capture_output=True, text=True, timeout=60)
        ok(r.returncode == 0, f'dash-growth --backfill exits 0 ({r.stderr[-200:]})')
        if r.returncode != 0:
            return
        g = json.load(open(out))
        days = {x['date']: x for x in g['days']}
        ok(sorted(days) == [d.isoformat() for d in (d1, d2, d3, today)], 'growth: one point per day, gaps carried forward, today live')
        a, b, c, t = (days[d.isoformat()] for d in (d1, d2, d3, today))
        ok(a['loc']['scripts'] == 5 and a['counts'] == {'scripts': 1, 'skills': 1, 'agents': 1, 'cron': 2}, 'growth: last commit of the day, counts')
        ok(a['claude_md_kb'] == 2.0 and a['loc']['skills'] == 4 and a['loc']['agents'] == 2 and a['loc']['notes'] == 1, 'growth: prompts and CLAUDE.md KB')
        ok(b['source'] == 'git (carried)' and b['loc'] == a['loc'], 'growth: a day without commits carries the previous state')
        ok(c['counts']['scripts'] == 2 and c['loc']['vault'] == 7, 'growth: vault from its own repo (code only, data ignored)')
        ok(t['source'] == 'live' and t['loc'] == c['loc'] and t['counts'] == c['counts'], 'growth: live tree matches the last git state')
        ok(g['activity'].get(d1.isoformat()) == {'heartbeats': 4, 'owner_msgs': 3, 'subagents': 3}, 'growth: activity from dash-data daily counts')
        ok('PRIVATE' not in open(out).read(), 'growth: numbers only')
        m0 = os.path.getmtime(out)
        subprocess.run([gb, '--if-stale', '55'], env=env, capture_output=True, timeout=60)
        ok(os.path.getmtime(out) == m0, 'growth: --if-stale skips a fresh file')
        g['days'] = [x for x in g['days'] if x['date'] != d2.isoformat()]          # a missed day + yesterday still 'live'
        for x in g['days']:
            if x['date'] == d3.isoformat():
                x['source'], x['loc'] = 'live', dict(x['loc'], scripts=999)
        with open(out, 'w') as f:
            json.dump(g, f)
        r = subprocess.run([gb], env=env, capture_output=True, text=True, timeout=60)
        days = {x['date']: x for x in json.load(open(out))['days']}
        ok(r.returncode == 0 and d2.isoformat() in days and days[d3.isoformat()]['source'] == 'git'
           and days[d3.isoformat()]['loc'] == t['loc'], 'growth: a new day rebuilds past days from git (gap filled, live point replaced)')
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------------------------------------------------- 10. DST hours
DST_CASES = [   # (name, pinned now in Zurich, the switch Sunday's expected hour labels or None for a normal day)
    ('autumn', (2026, 10, 25, 20, 0), ['00', '01', '02', '02′'] + [f'{h:02d}' for h in range(3, 24)]),
    ('autumn, day after', (2026, 10, 26, 1, 30), ['00', '01', '02', '02′'] + [f'{h:02d}' for h in range(3, 24)]),
    ('spring', (2027, 3, 28, 20, 0), ['00', '01'] + [f'{h:02d}' for h in range(3, 24)]),
    ('spring, day after', (2027, 3, 29, 1, 30), ['00', '01'] + [f'{h:02d}' for h in range(3, 24)]),
    ('normal day', (2026, 10, 18, 20, 0), [f'{h:02d}' for h in range(24)]),
]


def dst_now(t):
    return datetime(*t, tzinfo=ZoneInfo('Europe/Zurich'))


def dst_sunday(now, sunday):   # the switch Sunday's labels up to now (a case pinned on the Sunday evening ends there)
    return sunday if now.weekday() != 6 else [x for x in sunday if int(x[:2]) <= now.hour]


def test_hour_slots(m):
    """The 48 h hourly chart's slots with a pinned clock: real instants (3600 s apart), per-slot local labels, the
    fall-back hour twice (02:00, 02:00′), no spring-forward 02:00; tokens of the two autumn 02:00 hours stay apart."""
    Z = ZoneInfo('Europe/Zurich')
    for name, t, sunday in DST_CASES:
        now = dst_now(t)
        sl = m.hour_slots(now)
        sun = (now - dt.timedelta(days=1 if now.hour < 12 else 0)).date()
        sun = sun - dt.timedelta(days=(sun.weekday() + 1) % 7)   # the Sunday in the window
        labels = [x[2] for x in sl if x[1].startswith(sun.isoformat())]
        ok(len(sl) == 48 and all(b[0] - a[0] == 3600 for a, b in zip(sl, sl[1:])) and sl[-1][0] <= now.timestamp() < sl[-1][0] + 3600,
           f'hour slots ({name}): 48 real hours ending in now\'s hour')
        ok(all(lab[:5] == dt.datetime.fromtimestamp(e, Z).strftime('%H:00') and day == dt.datetime.fromtimestamp(e, Z).strftime('%a')
               and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d', key) for e, key, lab, day in sl), f'hour slots ({name}): local label, day and key per slot')
        ok([x.replace(':00', '') for x in labels] == dst_sunday(now, sunday), f'hour slots ({name}): {sun:%a %d.%m} labelled {labels[:5]}…')
    # cost pace: the share of the local day in real seconds (25 h autumn day, 23 h spring day)
    for t, fold, want in (((2026, 10, 25, 0, 0), 0, 0.0), ((2026, 10, 25, 2, 30), 0, 2.5 / 25), ((2026, 10, 25, 2, 30), 1, 3.5 / 25),
                          ((2026, 10, 25, 12, 0), 0, 13 / 25), ((2026, 10, 25, 23, 59), 0, (24 * 60 + 59) / (25 * 60)),
                          ((2027, 3, 28, 1, 59), 0, (60 + 59) / (23 * 60)), ((2027, 3, 28, 3, 0), 0, 2 / 23),
                          ((2027, 3, 28, 12, 0), 0, 11 / 23), ((2026, 10, 18, 12, 0), 0, 0.5)):
        got = m.day_fraction(datetime(*t, fold=fold, tzinfo=Z))
        ok(abs(got - want) < 1e-9, f'day_fraction {t} fold={fold}: {got:.4f} (want {want:.4f})')
    # end to end: collect_tokens with a pinned now buckets by instant
    tmp = tempfile.mkdtemp(prefix='dash-dst-')
    saved = (m.PROJECTS, m.JOBS, m.CACHE)
    try:
        proj = os.path.join(tmp, 'projects', '-home-node-workspace')
        os.makedirs(proj)
        stamps = {'a1': '2026-10-25T00:30:00Z', 'a2': '2026-10-25T01:30:00Z',   # 02:30 CEST, 02:30 CET
                  's1': '2027-03-28T00:30:00Z', 's2': '2027-03-28T01:30:00Z'}   # 01:30 CET, 03:30 CEST
        with open(os.path.join(proj, 'S1.jsonl'), 'w') as f:
            for i, (mid, ts) in enumerate(stamps.items()):
                f.write(json.dumps({'type': 'assistant', 'timestamp': ts, 'message': {
                    'id': mid, 'model': 'claude-opus-5-5', 'role': 'assistant', 'content': [{'type': 'text', 'text': 'x'}],
                    'usage': {'input_tokens': 0, 'output_tokens': 100 * (i + 1), 'cache_read_input_tokens': 0,
                              'cache_creation_input_tokens': 0}}}) + '\n')
        m.PROJECTS, m.JOBS, m.CACHE = os.path.join(tmp, 'projects'), os.path.join(tmp, 'jobs'), os.path.join(tmp, 'cache.json')
        for name, t, want in (('autumn', (2026, 10, 25, 20, 0), {'Sun 02:00': 100, 'Sun 02:00′': 200}),
                              ('spring', (2027, 3, 28, 20, 0), {'Sun 01:00': 300, 'Sun 03:00': 400})):
            tok = m.collect_tokens(use_cache=False, now=dst_now(t))
            hr = tok['hourly']
            ok(tok['cost_pace']['day_elapsed_pct'] == {'autumn': 84.0, 'spring': 82.6}[name],
               f"cost pace ({name}): day elapsed in real hours (21 of 25 / 19 of 23), got {tok['cost_pace']['day_elapsed_pct']}")
            got = {h['day'] + ' ' + h['label']: sum(h['tokens'].values()) for h in hr if sum(h['tokens'].values())}
            ok(len(hr) == 48 and got == want and (name != 'spring' or 'Sun 02:00' not in {h['day'] + ' ' + h['label'] for h in hr}),
               f'hourly chart ({name}): tokens in their real hour {got}')
    finally:
        m.PROJECTS, m.JOBS, m.CACHE = saved
        shutil.rmtree(tmp, ignore_errors=True)


def test_render_dst(tmpdir, m):
    """Page copies with a pinned 24 h timeline / 48 h chart across both DST switches (plus a normal day): every hour
    label sits on its own local hour, the quiet band runs 00:00–08:00 wall clock (9 h in autumn, 7 h in spring), and
    the hourly table shows 02:00 twice in autumn and not at all in spring."""
    Z = ZoneInfo('Europe/Zurich')
    cur = json.load(open(os.path.join(HOME, '.local', 'state', 'dash', 'data.json')))
    page = open(PAGE).read()
    days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    for name, t, sunday in DST_CASES:
        now = dst_now(t)
        c2 = json.loads(json.dumps(cur))
        frm = dt.datetime.fromtimestamp(now.timestamp() - 24 * 3600, Z)
        c2['activity']['timeline'] = {'from': frm.isoformat(timespec='seconds'), 'to': now.isoformat(timespec='seconds'),
                                      'runs': [], 'owner_messages': [{'at': now.isoformat(timespec='seconds')}]}
        c2['tokens']['hourly'] = [{'hour': k, 'label': lab, 'day': d, 'tokens': {}, 'cost_usd': 0} for _, k, lab, d in m.hour_slots(now)]
        payload = json.dumps(c2, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
        out = os.path.join(tmpdir, f'dst-{len(name)}-{t[1]}.html')
        with open(out, 'w') as f:
            f.write(re.sub(r'(<script id="dash-data"[^>]*>)(.*?)(</script>)', lambda x: x.group(1) + payload + x.group(3), page, count=1, flags=re.S))
        dom = chrome(['--window-size=390,2400', '--dump-dom', f'file://{out}#activity']).stdout
        svg = re.search(r'<svg[^>]*aria-label="Timeline of the last 24 hours[^>]*>.*?</svg>', dom, re.S)
        if not svg:
            ok(False, f'timeline ({name}): drawn')
            continue
        svg = svg.group(0)
        W = float(re.search(r'viewBox="0 0 ([\d.]+)', svg).group(1))
        t0, span = frm.timestamp(), 24 * 3600

        def at(px):   # px -> nearest whole hour (the inverse of the chart's x(), L = R = 6)
            return dt.datetime.fromtimestamp(round((t0 + (px - 6) / (W - 12) * span) / 3600) * 3600, Z)
        labels = [(float(x), lab) for x, lab in re.findall(r'<text x="([\d.]+)"[^>]*>(\d\d|[A-Z][a-z]{2})</text>', svg) if lab != 'You']
        bad = [lab for x, lab in labels if lab != (days[at(x).weekday()] if at(x).hour == 0 else f'{at(x).hour:02d}')]
        ok(len(labels) >= 6 and not bad, f'timeline ({name}): {len(labels)} hour labels on their local hour (off: {bad})')
        bands = [(float(x), float(w)) for x, w in re.findall(r'<rect x="([\d.]+)" y="[\d.]+" width="([\d.]+)"[^>]*fill:var\(--quiet\)', svg)]
        inner = [(at(x), at(x + w)) for x, w in bands if x > 7 and x + w < W - 7]
        hours = 9 if name.startswith('autumn') else 7 if name.startswith('spring') else 8
        ok(bands and all(at(x).hour == 0 for x, w in bands if x > 7) and all(at(x + w).hour == 8 for x, w in bands if x + w < W - 7)
           and all(round((b.timestamp() - a.timestamp()) / 3600) == hours for a, b in inner if a.date().weekday() == 6),
           f'timeline ({name}): quiet band 00:00–08:00 local ({[(a.strftime("%d %H"), b.strftime("%d %H")) for a, b in inner]})')
        dom = chrome(['--window-size=390,2400', '--dump-dom', f'file://{out}#usage']).stdout
        cells = re.findall(r'<td[^>]*>(Sun \d\d:00′?)</td>', dom)
        ok([c[4:].replace(':00', '') for c in cells] == dst_sunday(now, sunday), f'hourly table ({name}): Sunday rows {cells[:4]}…')


# ------------------------------------------------------------------------------------------- 6. render
def chrome(args, timeout=90, budget=4000):
    prof = tempfile.mkdtemp(prefix='dash-chrome-')
    try:
        return subprocess.run(['google-chrome', '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
                               '--disable-background-networking', '--disable-component-update', '--no-first-run',
                               '--hide-scrollbars', '--user-data-dir=' + prof, f'--virtual-time-budget={budget}'] + args,
                              capture_output=True, text=True, timeout=timeout)
    finally:
        shutil.rmtree(prof, ignore_errors=True)


SLO_IDS = ('tap_ack', 'first_reply', 'event_message', 'vault_answer', 'job_start_lag', 'reminder_late', 'joined_traces')


def test_hub_slo_static(page):
    """Hub card SLO tiles (P3-11): hubCard draws the hub snapshot's slo block through sloBlock(), as text only, with a
    fixed label for each of the 7 tile ids (packages/inbox/slo.py SPECS)."""
    m = re.search(r'// SLO tiles \(P3-11\).*?function hubCard\(H\)', page, re.S)
    js = m.group(0) if m else ''
    ok(bool(js) and 'innerHTML' not in js and 'insertAdjacentHTML' not in js, 'hub slo: drawn as text only (no HTML from data)')
    ok(all(f'{i}: ' in js for i in SLO_IDS), f'hub slo: a label for each tile id ({[i for i in SLO_IDS if f"{i}: " not in js]})')
    ok('sloBlock(H.slo)' in page, 'hub slo: hubCard draws the slo block')


def test_fixed_scope(page):
    css = page.split('</style>')[0]
    bare = re.findall(r'(?:^|[\s,}])\.live(?:\[[^\]]*\])?\s*\{[^}]*position:\s*fixed', css)
    ok(not bare and 'aside.live { position: fixed' in css, 'only the drawer (aside.live) is a fixed sheet')
    ok("'w live'" not in page, 'no store label carries the sheet class')


def test_ci_static(page):
    """CI tab (no cards since 30.10): drawn from D.ci / flowd's /live/ci, D.scorecard and D.systems.cron as text only; the
    Health hero counts the CI tab's problems (ciIssues); the stream runs only while the Health or CI tab is open; old hashes map."""
    m = re.search(r'// -+ CI tab: lands.*?// -+ CI: "does code land', page, re.S)
    js = m.group(0) if m else ''
    rc = re.search(r'function renderCI\(v\) \{.*?\n  \}\n', page, re.S)
    ok(bool(js) and 'innerHTML' not in js and 'insertAdjacentHTML' not in js, 'ci tab: drawn as text only (no HTML from data)')
    ok('ciIssues()' in js and 'function renderHealth(v)' in js and rc and 'sysCard(' not in js + rc.group(0) and 'ciCards' not in page,
       'ci tab: no cards; its problems are counted by the Health hero')
    ok("ciLive(view === 'health' || view === 'ci')" in page and "new EventSource('live/ci')" in js,
       'ci tab: live over /live/ci while the Health or CI tab is open')
    ok("system: 'health'" in page and "health: 'system'" not in page, 'ci tab: #system opens Health')


def test_doing_static(page):
    """Health tab's "How I'm doing": drawn as text from D.doing, D.traces, D.tokens and D.systems.cron; placed after the
    hero, before the board; no cards and no dot markers in it (the screenshot mask blacks out rows of round dots)."""
    m = re.search(r'// -+ "How I\'m doing".*?// -+ health: "is every part up', page, re.S)
    js = m.group(0) if m else ''
    ok(bool(js) and 'innerHTML' not in js and 'insertAdjacentHTML' not in js and 'dotk' not in js and 'sysCard(' not in js,
       'how doing: drawn as text only, no cards, no dot markers')
    rh = re.search(r'function renderHealth\(v\) \{.*?\n  \}\n', page, re.S)
    rh = rh.group(0) if rh else ''
    ok(0 < rh.find('v.appendChild(hero)') < rh.find('howDoing()') < rh.find('boardPanel()'), 'how doing: after the hero, before the board')


DOING_PROBE = """<script>setTimeout(function () {
  var b = document.getElementById('sys-doing'), r = b ? b.getBoundingClientRect() : null;
  document.body.setAttribute('data-probe', JSON.stringify({ sw: document.documentElement.scrollWidth, iw: innerWidth, w: r ? Math.round(r.width) : 0,
    spk: [].map.call(document.querySelectorAll('#sys-doing .spk svg, #sys-doing .spk .hd-mt'), function (e) { return Math.round(e.getBoundingClientRect().width); }),
    cols: [].map.call(document.querySelectorAll('#hd-week .cols'), function (e) { return Math.round(e.getBoundingClientRect().width); }) }));
}, 300);</script>"""


def test_doing_render(tmpdir):
    """Page copies with fixture doing, traces, tokens and cron blocks: the "How I'm doing" heading, the strip (answer speed
    p50 today with the scorecard's p95 sparkline and base, failures open vs fixed, today's work as lands / helpers / done
    meters, cost today with the cost-per-day sparkline and its regressed chip), Failures and blocks (red lands fixed vs
    still red, cron failing vs recovered, speed alerts, helpers, guard blocks, tool errors) and This week (8 day columns
    per row, today last); markup from data drawn as text; no page scroll at 375 px; older snapshots without the new
    parts still draw what they have."""
    cur = json.load(open(os.path.join(HOME, '.local', 'state', 'dash', 'data.json')))
    page = open(PAGE).read()
    days = ['2026-10-%02d' % d for d in range(23, 31)]
    doing = {'at_local': '14:07', 'checked': 9, 'over': [{'id': 'cost_day', 'status': 'regressed'}, {'id': '<b>zzm</b>', 'status': 'high'}],
             'metrics': [{'id': 'reply_p95', 'unit': 's', 'value': 31.1, 'base': 46.2, 'n': 29, 'window_h': 6, 'status': 'ok',
                          'series': [None] * 4 + [20.0 + i for i in range(44)]},
                         {'id': 'cost_day', 'unit': 'usd', 'value': 725.8, 'base': 224.6, 'n': 450, 'window_h': 24, 'status': 'regressed',
                          'series': [300.0 + 9 * i for i in range(48)]}],
             'lands': {'days': [{'day': d, 'n': 10 + i, 'green': 6 + i} for i, d in enumerate(days)], 'red_today': 5, 'red_branches': 3, 'fixed': 2, 'open': 1},
             'done': {'days': [{'day': d, 'n': i * 2} for i, d in enumerate(days)]}}
    trd = [{'day': d, 'main': 1.0, 'agents': 2.0, 'turns': 50, 'runs': 6 + i, 'replies': 10, 'reply_p50_ms': 9000 + 1000 * i,
            'reply_p95_ms': 30000, 'tools': 1000, 'errors': 10 + i, 'hook_blocks': 2, 'agents_bad': 1 if i == 7 else 0} for i, d in enumerate(days)]
    traces = {'tiles': {'cost_24h': 700.0, 'cost_7d': 1600.0, 'owner_reply_p50_ms': 11111.0, 'owner_reply_p95_ms': 40000.0, 'error_rate_24h': 0.02,
                        'hook_blocks_24h': 9, 'agents_24h': 20, 'agents_bad_24h': 2, 'turns_24h': 300},
              'daily': trd, 'errors': {'rate': 0.012}, 'by_trigger': [], 'agents': [], 'slow': [], 'latency': {}}
    tk = dict(cur.get('tokens') or {})
    tk['today'] = dict(tk.get('today') or {}, date=days[-1], cost_usd=123.4, subagent_runs=13, tokens={'main': 4e8, 'subagent': 6e8, 'headless': 0})
    tk['daily'] = [dict(tk['today'], date=d, cost_usd=50.0 + 10 * i, subagent_runs=5 + i) for i, d in enumerate(days[:-1])] + [dict(tk['today'])]
    cron = {'status': 'warn', 'wrapped': 100, 'failing': [{'job': 'zzjob', 'since': '13:00'}], 'summary': '1 job failing',
            'heat': {'hours': list(range(24)), 'jobs': [{'job': '<b>zzc</b>', 'failing': False, 'runs': [1] * 24, 'fail': [0] * 23 + [1]}],
                     'ok': [['zzok', '1' * 24]], 'n_jobs': 10, 'runs': 240, 'failed': 4, 'recovered': 3}}

    def render(name, width=1280, extra='', drop=()):
        c2 = json.loads(json.dumps(cur))
        c2.update(doing=doing, traces=traces, tokens=tk, generated_local=days[-1] + ' 14:10')
        c2['systems'] = dict(c2.get('systems') or {}, cron=cron)
        for k in drop:
            c2[k] = None
        payload = json.dumps(c2, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
        out = os.path.join(tmpdir, f'doing-{name}.html')
        p2 = re.sub(r'(<script id="dash-data"[^>]*>)(.*?)(</script>)', lambda x: x.group(1) + payload + x.group(3), page, count=1, flags=re.S)
        if extra:
            i = p2.rindex('</body>')
            p2 = p2[:i] + extra + p2[i:]
        with open(out, 'w') as f:
            f.write(p2)
        dom = chrome([f'--window-size={width},2400', '--dump-dom', f'file://{out}#health']).stdout
        a_ = dom.find('id="view-health"')
        return dom, (dom[a_:dom.find('</section>', a_)] if a_ >= 0 else '')

    def part(v, key, end):
        a_ = v.find(key)
        return v[a_:v.find(end, a_ + 10)] if a_ >= 0 else ''

    dom, v = render('full')
    b = part(v, 'id="sys-doing"', '<article')
    ok('<div class="err"' not in dom and b and ">How I'm doing<" in b.replace('&#39;', "'"), 'how doing: renders on Health, before the board')
    ok('<article' not in b and 'dotk' not in b and '<circle' not in b, 'how doing: no cards, no dot markers')
    ok(b.count('class="spk') == 4 and all(f'data-hd="{k}"' in b for k in ('speed', 'fails', 'work', 'cost')), 'how doing: a strip of four tiles')
    sp, fl, wk, co = (part(b, f'data-hd="{k}"', '</div></div>') for k in ('speed', 'fails', 'work', 'cost'))
    ok('>16 s<' in sp and 'p95 31 s · base 46 s' in sp and sp.count('<path class="ln"') == 1 and 'class="bl"' in sp and 'class="chip' not in sp,
       f'how doing: answer speed = today\'s p50, the scorecard p95 sparkline over its base ({sp[:300]})')
    ok('>4 open<' in fl and '5 fixed · 2 blocks' in fl and 'class="g"' in fl and 'class="r"' in fl,
       'how doing: failures open (red land, failing cron job, an alert) vs fixed (green again, recovered)')
    ok('>6 lands<' not in wk and '>13 lands<' in wk and '13 helpers · 14 done' in wk and wk.count('class="m"') == 3 and 'class="tk"' in wk,
       'how doing: today\'s work: green lands, helpers, done as meters with the 7-day average tick')
    ok('>$123<' in co and 'base $225/day' in co and '>regressed<' in co and '1B tokens' in co and co.count('<path class="ln"') == 1,
       'how doing: cost today (the page\'s one cost answer) and the cost-per-day sparkline, regressed')
    fs = part(b, 'id="hd-fails"', 'id="hd-week"')
    rows = re.findall(r'data-row="([a-z]+)"', fs)
    ok(rows == ['lands', 'cron', 'alerts', 'helpers', 'blocks', 'errors'], f'how doing: the failure rows in order ({rows})')
    ok(all(w in fs for w in ('2 of 3 branches fixed', '1 failing · 3 of 4 recovered', 'cost/day, &lt;b&gt;zzm&lt;/b&gt;', '1 of 13 ended badly',
                             '2 · avg 2/day', '1.7% · 7 d 1.2%')) and '<b>zz' not in fs,
       f'how doing: fixed vs open per kind, today\'s counts, ids drawn as text ({re.sub("<[^>]+>", " ", fs)[:600]})')
    ok('class="hd-row bad" data-row="errors"' not in fs and 'class="hd-row bad" data-row="cron"' in fs and 'class="ci-sec warn" id="hd-fails"' in b,
       'how doing: tool errors within 1.5x their week stay calm; open problems mark the section')
    we = part(b, 'id="hd-week"', 'class="note')
    wr = re.findall(r'data-wk="([a-z]+)"', we)
    ok(wr == ['reply', 'cost', 'lands', 'helpers', 'done', 'errors'] and len(re.findall(r'class="col( today)?"', we)) == 48 and we.count('class="col today"') == 6,
       f'how doing: the week as 6 rows of 8 day columns, today last ({wr})')
    ok(all(w in we for w in ('>16 s<', '>$123<', '>13/17<', '>13<', '>14<', '>17 · 2<', '>30<', '23 Oct – today')),
       'how doing: each week row ends with today\'s number')
    dom, _ = render('phone', width=375, extra=DOING_PROBE)
    m = re.search(r'data-probe="([^"]*)"', dom)
    pr = json.loads(m.group(1).replace('&quot;', '"').replace('&amp;', '&')) if m else {}
    ok(pr.get('w', 0) > 0 and pr.get('sw', 1) <= pr.get('iw', 0) and min(pr.get('spk') or [0]) >= 70 and min(pr.get('cols') or [0]) >= 120,
       f'how doing: no page scroll at 375 px, sparklines and week columns keep their width ({pr})')
    dom, v = render('old', drop=('doing', 'traces'))
    b = part(v, 'id="sys-doing"', '<article')
    ok('<div class="err"' not in dom and b and '>$123<' in b and 'data-hd="speed"' not in b and 'data-wk="lands"' not in b and 'data-wk="cost"' in b,
       'how doing: an older snapshot (no doing, no traces) draws what it has, nothing invented')


CI_PROBE = """<script>setTimeout(function () {
  document.body.setAttribute('data-probe', JSON.stringify({ sw: document.documentElement.scrollWidth, iw: innerWidth,
    bar: document.querySelectorAll('#view-ci .ci-seg').length }));
}, 300);</script>"""


HEAD_PROBE = """<script>setTimeout(function () {
  var hd = [].map.call(document.querySelectorAll('#ci-suites .ci-mx .hd'), function (e) {
    var r = e.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; });
  document.body.setAttribute('data-probe', JSON.stringify({ n: hd.length, hd: hd,
    dots: document.querySelectorAll('#ci-suites .ci-mx .hd .dotk, #ci-suites .ci-mx .hd.dotk').length }));
}, 300);</script>"""


ABOUT_PROBE = """<script>setTimeout(function () {
  function box(e) { var r = e ? e.getBoundingClientRect() : null; return r ? [Math.round(r.left), Math.round(r.top), Math.round(r.right), Math.round(r.bottom)] : null; }
  var d = document.querySelector('#ci-lands details.land[data-k$="|zzci-steps"]');
  if (d) d.open = true;
  setTimeout(function () {
    var a = d && d.querySelector('.ci-steps .about'), g = d && d.querySelector('.ci-steps .grp'), sm = d && d.querySelector('summary');
    document.body.setAttribute('data-probe', JSON.stringify({ about: box(a), grp: box(g), stp: box(d && d.querySelector('.ci-steps .stp')),
      sum: box(sm), sw: document.documentElement.scrollWidth, iw: innerWidth }));
  }, 100);
}, 300);</script>"""


SCROLL_PROBE = """<script>setTimeout(function () {
  var out = { boxes: [], sw: document.documentElement.scrollWidth, iw: innerWidth };
  document.querySelectorAll('#view-ci .ci-scroll').forEach(function (b) {
    b.scrollTop = Math.round(b.scrollHeight / 2); b.scrollLeft = 40;
    var r = b.getBoundingClientRect(), ft = b.querySelectorAll('.ft'), ax = ft[ft.length - 1].getBoundingClientRect(),
        lb = b.querySelectorAll('.lb.mono')[20].getBoundingClientRect();
    out.boxes.push({ h: Math.round(r.height), sh: b.scrollHeight, w: b.clientWidth, sw: b.scrollWidth, bottom: Math.round(r.bottom - (b.offsetHeight - b.clientHeight)),
                     ax: Math.round(ax.bottom), left: Math.round(r.left), lb: Math.round(lb.left) });
  });
  document.body.setAttribute('data-probe', JSON.stringify(out));
}, 300);</script>"""


STEPS_PROBE = """<script>setTimeout(function () {
  var d = document.querySelector('#ci-lands details.land[data-k*="zzci-steps"]');
  if (d) d.open = true;
  setTimeout(function () {
    var t = d && d.querySelector('.tlb'), nm = [].map.call(d ? d.querySelectorAll('.ci-steps .nm') : [], function (e) { return Math.round(e.getBoundingClientRect().right); });
    document.body.setAttribute('data-probe', JSON.stringify({ open: !!(d && d.open), sw: document.documentElement.scrollWidth, iw: innerWidth,
      rows: d ? d.querySelectorAll('.ci-steps .st').length : 0, tlh: t ? Math.round(t.getBoundingClientRect().height) : 0, nm: nm,
      dots: document.querySelectorAll('#ci-lands .tlb .dotk, #ci-lands .ci-steps .dotk').length }));
  }, 100);
}, 300);</script>"""


def test_ci_render(tmpdir):
    """Page copies with fixture ci, scorecard and cron blocks: the CI tab without cards: the status bar (main red, the
    queue, the last deploy, runner, nightly, cron), the speed strip, Lands (24 h chart with deploy and main-run markers,
    the runs table with queue rows, each land a timeline bar (local CI and runner lanes, wait, merge + deploy) and a fold
    with its steps, older lands folded), Suites on main (suites x runs) and the cron
    heatmap; markup drawn as text; no page scroll at 375 px; the Health hero counts the CI problems and opens the CI tab;
    no ci block -> no Lands section."""
    cur = json.load(open(os.path.join(HOME, '.local', 'state', 'dash', 'data.json')))
    page = open(PAGE).read()
    iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone(dt.timedelta(hours=1))).isoformat(timespec='seconds')
    now = 1793206800
    land = lambda b, ok_, **k: dict({'t': iso(now - 600), 'branch': b, 'ok': ok_, 'total_s': 276 if ok_ else None, 'local_s': 126.0,
                                     'runner_s': 264.6, 'runner': 'signed', 'sel': 'impacted', 'files': 5, 'of': 137, 'tests': 76,
                                     'reruns': 0, 'rerun_ok': None, 'quarantined': 0, 'failed': []}, **k)
    steps = land('zzci-steps', True, total_s=353, desc='CI tab lands: a short description per land', commits=1,
                 body=['The fold shows what a land was about: its subject and body.', '- item <b>two</b>'], local_s=340.6, runner_s=208.4, merge_s=12, wait_s=6, queue_s=4,
                 tl={'span': 353, 'segs': [['local', 0, 340.6], ['queue', 1, 4], ['runner', 5, 208.4], ['lock', 335, 6], ['merge', 341, 12]]},
                 steps={'runner': [['pytest-3.13', 191.1], ['legacy', 168.7], ['pyright', 29.1], ['ruff', 0.1], ['bun', 0.0]],
                        'local': [['legacy', 250.2], ['<b>zzstep</b>', 3.0]]})
    lands = [land('zzci-red', False, failed=['runner pytest-3.13', 'legacy zzsuite'], runner='red'), steps, land('zzci-green', True, reruns=1, rerun_ok=True),
             land('<b>zzmark</b>', True, desc='<b>zzdesc</b> markup as text', commits=1, body=[]),
             land('zzci-many', True, desc='First of seven +6 more', commits=7,
                  body=['zzsub %d' % i for i in range(5)] + ['+2 more'])] + [land('old-%d' % i, True) for i in range(5)]
    rate = lambda n, g, m: {'n': n, 'green': g, 'share': round(g / n, 3), 'median_s': m}
    ci = {'now': iso(now), 'queue': {'holder': {'op': 'land', 'branch': 'zzci-holder', 'since': iso(now - 120)},
                                'waiting': [{'op': 'land', 'branch': 'zzci-next', 'since': iso(now - 60)}],
                                'starting': [{'op': 'land', 'branch': 'zzci-phase', 'since': iso(now - 900), 'phase': 'CI host + runner',
                                              'phase_since': iso(now - 240), 'rerun': 1},
                                             {'op': 'land', 'branch': 'zzci-bare', 'since': None, 'phase': None, 'rerun': 0}]},
          'lands': lands, 'headline': {'today': rate(10, 4, 290), 'week': rate(50, 30, 300), 'before': rate(40, 25, 390),
                                       'after': rate(8, 5, 276), 'speedup': {'commit': '696a6a7', 'at': iso(now - 3600)}},
          'nightly': {'scheduled': True, 'next': iso(now + 30000), 'last': {'t': iso(now - 50000), 'ok': False, 'secs': 410, 'head': 'abc',
                                                                      'red': ['pytest-3.13', 'legacy']}},
          'runner': {'gate': 'enforce-gated', 'signed_green_lands': 15,
                     'record': {'t': iso(now - 700), 'ok': True, 'signed': True, 'release': 'c6215512a696', 'toolchain': 'x',
                                'python': '3.13.15', 'secs': 264.6, 'branch': 'zzci-green', 'failed': [], 'workers': 1,
                                'workers_note': 'serial (release before the CI speed-up)'}},
          'legacy': {'suites': 140, 'red_n': 1, 'red': [{'suite': 'zzsuite', 't': iso(now - 300), 'how': 'post-land'}],
                     'last': {'t': iso(now - 300), 'ok': False, 'secs': 164, 'after': 'zzci-green', 'head': 'eeee', 'red': ['zzsuite']},
                     'skipped': {'t': iso(now - 100), 'why': 'load 33'}, 'runs_7d': 12, 'green_7d': 10,
                     'grid': {'runs': [{'t': iso(now - 300 * (12 - i)), 'ok': i != 11, 'head': 'h%d' % i, 'after': 'b%d' % i} for i in range(12)],
                              'rows': [{'suite': 'zzsuite', 'red_now': True, 'cells': [0] * 11 + [1]},
                                       {'suite': '<i>zzold</i>', 'red_now': False, 'cells': [1] + [0] * 11}],
                              'green': ['zzg%03d' % i for i in range(150)]}},
          'deploys': {'core': {'release': 'aaaaaaaaaaaa', 't': iso(now - 500), 'files': 329}, 'hub': {'release': 'bbbbbbbbbbbb'},
                      'recent': [{'t': iso(now - 400), 'target': 'hub', 'ok': False, 'release': 'cccccccccccc'},
                                 {'t': iso(now - 9000), 'target': 'hub', 'ok': True, 'release': 'bbbbbbbbbbbb'}], 'n_7d': 5, 'failed_7d': 1},
          'timeline': {'at': iso(now - 23 * 3600), 'hours': [{'at': iso(now - (23 - i) * 3600), 'green': i % 3, 'red': 2 if i == 22 else 0} for i in range(24)],
                       'marks': [{'t': iso(now - 50000), 'kind': 'nightly', 'ok': False, 'target': None},
                                 {'t': iso(now - 9000), 'kind': 'deploy', 'ok': True, 'target': 'hub'},
                                 {'t': iso(now - 400), 'kind': 'deploy', 'ok': False, 'target': 'hub'},
                                 {'t': iso(now - 300), 'kind': 'legacy', 'ok': False, 'target': None}]}}
    cron = {'status': 'warn', 'wrapped': 129, 'failing': [{'job': 'zzjob', 'since': '17:00'}], 'runs_on': {'hub': 2, 'cron': 129},
            'summary': '1 job failing',
            'heat': {'hours': list(range(19, 24)) + list(range(0, 19)),
                     'jobs': [{'job': 'zzjob', 'failing': True, 'runs': [12] * 24, 'fail': [0] * 20 + [1, 5, 12, 12]},
                              {'job': '<b>zzcron</b>', 'failing': False, 'runs': [1] * 24, 'fail': [0] * 23 + [1]}],
                     'ok': [['zzok%02d' % i, '1' * 22 + 'z0'] for i in range(70)], 'n_jobs': 72, 'runs': 2700, 'failed': 33}}
    sc = {'at_local': '17:07', 'metrics': [
        {'id': 'land_p95', 'unit': 's', 'value': 412.5, 'base': 240.0, 'n': 12, 'window_h': 12, 'status': 'regressed',
         'series': [None] * 10 + [200.0 + i * 5 for i in range(38)]},
        {'id': 'land_fail', 'unit': 'share', 'value': 0.25, 'base': 0.3, 'n': 20, 'window_h': 12, 'status': 'ok', 'series': [0.3] * 48},
        {'id': 'cron_fail', 'unit': 'share', 'value': None, 'base': None, 'n': 0, 'window_h': 6, 'status': 'no-data', 'series': []}]}

    def render(block, name, view='ci', score=sc, cr=cron, width=1280, extra=''):
        c2 = json.loads(json.dumps(cur))
        c2['ci'] = block
        c2['scorecard'] = score
        c2['systems'] = dict(c2.get('systems') or {}, cron=cr)
        payload = json.dumps(c2, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
        out = os.path.join(tmpdir, f'ci-{name}.html')
        p2 = re.sub(r'(<script id="dash-data"[^>]*>)(.*?)(</script>)', lambda x: x.group(1) + payload + x.group(3), page, count=1, flags=re.S)
        if extra:
            i = p2.rindex('</body>')
            p2 = p2[:i] + extra + p2[i:]
        with open(out, 'w') as f:
            f.write(p2)
        dom = chrome([f'--window-size={width},2400', '--dump-dom', f'file://{out}#{view}']).stdout
        a_ = dom.find(f'id="view-{view}"')
        return dom, (dom[a_:dom.find('</section>', a_)] if a_ >= 0 else '')

    def part(v, id_, end='id="ci-'):
        a_ = v.find(f'id="{id_}"')
        if a_ < 0:
            return ''
        b_ = v.find(end, a_ + 10)
        a_ = v.rfind('<', 0, a_)   # from the element's own tag (its class comes before its id)
        return v[a_:b_ if b_ > 0 else len(v)]

    dom, v = render(ci, 'full')
    ok('<div class="err"' not in dom and 'id="ci-main"' in v, 'ci tab: renders the status bar')
    ok('<article' not in v and 'class="sys ' not in v and 'class="stats' not in v, 'ci tab: no cards, no stat tiles')
    want = {'ci-main': ['ci-seg main critical', '>Main<', '>red<', '>zzsuite<'],
            'ci-queue': ['dotk run', '>zzci-holder<', 'landing · 3 more'],
            'ci-deploy': ['core aaaaaaa', 'hub bbbbbbb', 'failed', '329 core files', '5 hub deploys in 7 d, 1 failed'],
            'ci-runner': ['green · 4.4 min', 'enforce-gated', 'release c6215512a696', '1 worker', 'Python 3.13.15', '15 signed green lands'],
            'ci-nightly': ['ci-seg nightly warn', 'red · ', 'red: pytest-3.13, tool suites', 'next '],
            'ci-cronnow': ['ci-seg cron warn', '1 job failing', '33 failed runs in 24 h']}
    for k, words in want.items():
        c = part(v, k)
        ok(c and all(w in c for w in words), f'ci bar: {k} ({[w for w in words if w not in c]})')
    sp = part(v, 'ci-speed')
    ok(sp.count('data-speed="') == 3 and sp.count('<path class="ln"') == 2 and sp.count('class="bl"') == 2
       and all(w in sp for w in ('Land p95', 'Failed lands', 'Failed cron', '>regressed<', '>no data<', '6.9 min', 'base 4.0 min', '25%', 'run 17:07')),
       'ci speed: three compact sparklines; a line and a baseline where there is data')
    lf = sp[sp.find('data-speed="land_fail"'):sp.find('data-speed="cron_fail"')]
    ok(lf and 'class="chip' not in lf and 'base 30%' in lf, 'ci speed: a metric within its baseline carries no chip')
    ok(sp.count('<path class="end"') == 2 and '<circle' not in sp and sp.count('visibility="hidden"') == sp.count('class="cur"') > 0,
       'ci speed: the end dot is a round-capped stroke; the hover line starts hidden')
    ln = part(v, 'ci-lands')
    ok(all(w in ln for w in ('>Lands<', '>40%</b> green today', '>60%</b> green 7 d', '>4.6 min</b> median land')), 'ci lands: green today, 7 d, median')
    tl = ln[ln.find('class="ci-tl"'):ln.find('class="ci-leg"')]
    ok(tl.count('class="col"') == 24 and tl.count('class="mk ') == 4 and 'mk deploy bad' in tl and 'mk nightly bad' in tl
       and 'mk legacy bad' in tl and 'title="hub deploy · ' in tl and '>now<' in tl and '6 green, 2 red' not in tl and '1 green, 2 red' in tl,
       'ci lands: 24 hourly bars, deploy and main-run markers, red ones marked')
    ok(ln.count('data-q="') == 4 and 'CI host + runner 4 min · re-run #1 (main moved)' in ln and '>starting<' in ln and 'queued #1' in ln,
       'ci lands: the queue on top of the table (holder, waiting, starting with its phase)')
    ok(ln.count('data-land="red"') == 1 and ln.count('data-land="green"') == 9 and '4 older lands' in ln and '<details' in ln,
       'ci lands: one row per land, red marked, older lands folded')
    ok(all(w in ln for w in ('failed: runner pytest-3.13, tool suite zzsuite', '1 rerun', 'impacted 5/137 files', 'local CI 2.1 min · runner 4.4 min (runner signed)',
                             'sg runner red', '>4.6 min<')), 'ci lands: failed step, selection, local/runner lanes and the land time')
    # a land's timeline (cifeed tl): local CI and the runner as two lanes, wait and merge + deploy across both; its fold:
    # each phase's time, then the runner's and the local CI's steps, slowest first, as bars
    i_ = ln.find('data-k="land|' + iso(now - 600) + '|zzci-steps"')
    one = ln[ln.rfind('<details', 0, i_):ln.find('</details>', i_)] if i_ >= 0 else ''
    bar_ = one[one.find('class="tlb"'):one.find('class="d"')]
    ok(one.count('class="ln ') == 2 and all(w in bar_ for w in ('class="sg local" style="left:0.00%;width:96.49%"', 'class="sg queue"', 'class="sg runner"',
                                                                 'class="sg lock"', 'class="sg merge" style="left:96.60%;width:3.40%"'))
       and 'local CI 5.7 min · runner 3.5 min · wait 10 s · merge + deploy 12 s' in bar_ and '>5.9 min<' in one and 'width:100.0%' in bar_,
       f'ci lands: the timeline bar (two lanes, wait, merge + deploy; the widest land fills the column) ({bar_[:400]})')
    fold = one[one.find('class="ci-steps'):one.find('class="about"')]
    names = re.findall(r'class="nm">([^<]*)<', fold)
    ok(names == ['pytest-3.13', 'tool suites', 'pyright', 'ruff', 'bun', 'tool suites', '&lt;b&gt;zzstep&lt;/b&gt;']
       and all(w in fold for w in ('>runner</b> 3.5 min', '>local CI</b> 5.7 min', '>3.2 min<', '>2.8 min<', '>29 s<', '>0 s<', 'merge + deploy <b>12 s</b>', 'wait <b>10 s</b>'))
       and fold.count('class="bar rn"') == 5 and fold.count('class="bar lc"') == 2 and 'zzstep</b>' not in fold,
       f'ci lands: the fold lists the runner and local steps, slowest first, "tool suites" for legacy ({names})')
    ok(ln.count('class="tlb"') == 10 and ln.count('<details class="land"') == 10, 'ci lands: every land row is a fold with a timeline')
    # what the land was about sits in its fold (not the collapsed row): one commit = subject + body lines, several =
    # each subject; desktop right of the steps, phone under them
    ok('class="desc"' not in ln and 'has-desc' not in ln and 'class="about"' not in ln.split('<details')[0]
       and ln.count('class="about"') == 3 and ln.count('class="ci-steps has-about"') == 3, 'ci lands: the collapsed rows carry no description; 3 folds do')
    ab = one[one.find('class="about"'):]
    ok('>CI tab lands: a short description per land<' in ab and 'class="body"' in ab and '- item &lt;b&gt;two&lt;/b&gt;' in ab
       and '<b>two' not in ab and '>commit<' in ab, f'ci lands: the fold names the commit, its subject and body lines as text ({ab[:300]})')
    i_ = ln.find('|zzci-many"')
    mn = ln[i_:ln.find('</details>', i_)]
    ok('>7 commits<' in mn and mn.count('<li>') == 6 and '<li>zzsub 0</li>' in mn and '<li>+2 more</li>' in mn and 'class="body"' not in mn,
       'ci lands: a fold of several commits lists each subject')
    for w_, nm_ in ((1280, 'about-desktop'), (375, 'about-phone')):
        dom, _ = render(ci, nm_, width=w_, extra=ABOUT_PROBE)
        m = re.search(r'data-probe="([^"]*)"', dom)
        pr = json.loads(m.group(1).replace('&quot;', '"').replace('&amp;', '&')) if m else {}
        a_, g_, s_ = pr.get('about'), pr.get('grp'), pr.get('stp')
        if not (a_ and g_ and s_):
            good = False
        elif w_ > 760:
            good = a_[0] >= s_[2] and a_[0] - pr['sum'][0] >= (pr['sum'][2] - pr['sum'][0]) * 0.4 and a_[1] <= g_[1]
        else:
            good = a_[1] >= s_[3] and abs(a_[0] - s_[0]) <= 1
        ok(good and pr.get('sw', 1) <= pr.get('iw', 0), f'ci lands: the fold at {w_} px: the description right of the steps (desktop) / under them (phone) ({pr})')
    ok('<b>zzmark</b>' not in v and '&lt;b&gt;zzmark' in v and '<b>zzcron</b>' not in v and '<i>zzold</i>' not in v,
       'ci tab: branch, suite and job names with markup are drawn as text')
    su = part(v, 'ci-suites')
    ok(su.count('class="c r"') == 2 and su.count('class="c g"') == 22 + 150 * 12 and 'class="c o"' not in su and 'lb mono bad' in su
       and su.count('class="lb mono') == 152 and su.find('data-suite="zzsuite"') < su.find('data-suite="zzg000"') < su.find('data-suite="zzg149"')
       and 'others' not in su and 'skipped' in su and '>10/12</b>' in su and 'repeat(12,' in su and 'class="ci-scroll suites"' in su,
       'ci suites: every suite x 12 main runs, red first, in a scroll area')
    cr = part(v, 'ci-cron')
    hg = cr[:cr.find('class="ci-leg"')]   # the grid without its legend
    ok(hg.count('class="c f3"') == 3 and hg.count('class="c f2"') == 1 and hg.count('class="c f1"') == 1 and 'zzjob (failing now)' in cr
       and 'other job' not in cr and '>2,700</b> runs' in cr and '1 failing now' in cr.replace('</b> ', ' ').replace('<b class="bad">', '')
       and hg.count('class="lb mono') == 72 and hg.find('>zzjob<') < hg.find('>zzok00<') < hg.find('>zzok69<') and 'zzok00 · 23:00 · 1 run, all ok' in hg
       and hg.count('35+ runs, all ok') == 70 and 'repeat(24,' in cr and 'no run' in cr and 'class="ci-scroll cron"' in cr,
       'ci cron: every job x 24 hours, failures by share, failing now first, in a scroll area')
    # both heatmaps scroll inside a box about a dozen rows high; the axis stays at its bottom, the names on its left
    for w_, nm_ in ((1280, 'scroll-desktop'), (375, 'scroll-phone')):
        dom, _ = render(ci, nm_, width=w_, extra=SCROLL_PROBE)
        m = re.search(r'data-probe="([^"]*)"', dom)
        pr = json.loads(m.group(1).replace('&quot;', '"').replace('&amp;', '&')) if m else {}
        good = len(pr.get('boxes') or []) == 2 and all(150 <= b['h'] <= 270 and b['sh'] > 2 * b['h'] and abs(b['ax'] - b['bottom']) <= 2
                                                        and abs(b['lb'] - b['left']) <= 1 for b in pr['boxes'])
        ok(good and pr.get('sw', 1) <= pr.get('iw', 0) and (w_ > 760 or pr['boxes'][1]['sw'] > pr['boxes'][1]['w']),
           f'ci heatmaps at {w_} px: a scroll box, axis at its bottom and names on its left while scrolled (cron sideways on a phone) ({pr})')
    ok(0 < v.find('class="ci-bar"') < v.find('id="ci-speed"') < v.find('id="ci-lands"') < v.find('id="ci-suites"') < v.find('id="ci-cron"'),
       'ci tab: status bar, speed strip, lands, suites, cron')
    ok('>Tool suites on main<' in v and 'Legacy' not in v and 'legacy suite' not in v.lower() and 'failed: legacy' not in v,
       'ci tab: the legacy suites read "tool suites" on the page')
    dom, _ = render(ci, 'phone', width=375, extra=CI_PROBE)
    m = re.search(r'data-probe="([^"]*)"', dom)
    pr = json.loads(m.group(1).replace('&quot;', '"').replace('&amp;', '&')) if m else {}
    ok(pr.get('bar') == 6 and pr.get('sw', 1) <= pr.get('iw', 0), f'ci tab: no page scroll at 375 px ({pr})')
    dom, _ = render(ci, 'phone-open', width=375, extra=STEPS_PROBE)
    m = re.search(r'data-probe="([^"]*)"', dom)
    pr = json.loads(m.group(1).replace('&quot;', '"').replace('&amp;', '&')) if m else {}
    ok(pr.get('open') and pr.get('sw', 1) <= pr.get('iw', 0) and pr.get('rows') == 7 and 0 < pr.get('tlh', 0) <= 16
       and all(0 < w < 380 for w in pr.get('nm', [])) and pr.get('dots') == 0,
       f'ci lands: an opened land at 375 px: its steps in view, no page scroll, the timeline a thin bar, no dot row ({pr})')
    # the run results above the suites grid are short bars, not a row of round dots: the browser's screenshot mask OCRs
    # evenly spaced dots as characters and blacked the row out at desktop width
    dom, _ = render(ci, 'heads', extra=HEAD_PROBE)
    m = re.search(r'data-probe="([^"]*)"', dom)
    pr = json.loads(m.group(1).replace('&quot;', '"').replace('&amp;', '&')) if m else {}
    ok(pr.get('n') == 12 and pr.get('dots') == 0 and all(w >= 2 * h_ and 0 < h_ <= 6 for w, h_ in pr.get('hd', [])),
       f'ci suites: each run is a short bar at desktop width, no dot row ({pr})')
    dom, hv = render(ci, 'hero', 'health')
    hero = hv[hv.find('class="sys-hero'):hv.find('id="sys-board"')]
    ok(hero.count('data-tab="ci"') >= 5 and 'CI · Main' in hero and 'red on main: zzsuite' in hero and 'Open the CI tab' in hero,
       f'health hero: the CI problems counted, each opens the CI tab ({hero.count("data-tab=")})')
    ok('data-go="ci"' in hero and 'CI · landing zzci-holder' in hero, 'health hero: one CI chip (what is landing)')
    ok('id="ci-main"' not in hv and 'id="ci-cron"' not in hv, 'health: nothing of the CI tab on the Health tab')
    dom, v = render(None, 'none', score=None)
    ok('<div class="err"' not in dom and 'id="ci-lands"' not in v and 'id="ci-cron"' in v and 'data-nocount="1"' in part(v, 'ci-speed'),
       'ci tab: no ci block -> no Lands; cron stays; missing speed data is not counted as a problem')


def test_hub_slo_render(tmpdir):
    """Page copies with a fixture hub snapshot (hub_mode snapshot, no stream): the 7 tiles with value, target and status
    chip plus the met count; a stale block; a tile with markup in its id and a non-number value (drawn as text, '–');
    a snapshot without slo (no section)."""
    cur = json.load(open(os.path.join(HOME, '.local', 'state', 'dash', 'data.json')))
    page = open(PAGE).read()
    base = cur.get('hub') if isinstance(cur.get('hub'), dict) and cur['hub'].get('parts') else \
        {'at': 1, 'parts': [{'id': 'claude-hub', 'label': 'claude-hub', 'kind': 'container', 'state': 'ok'}]}
    t = lambda i, v, tg, op, st, unit='s', n=10: {'id': i, 'value': v, 'target': tg, 'op': op, 'unit': unit, 'n': n,
                                                  'status': st, 'window_days': 7.0}
    tiles = [t('tap_ack', 0.1, 1.0, '<', 'ok'), t('first_reply', 49.11, 20.0, '<', 'miss'),
             t('event_message', None, 60.0, '<', 'no-data', n=0), t('vault_answer', 119.19, 10.0, '<', 'miss'),
             t('job_start_lag', 9.42, 10.0, '<=', 'ok'), t('reminder_late', 3.1, 30.0, '<=', 'ok'),
             t('joined_traces', 1.0, 0.95, '>=', 'ok', unit='share')]

    def render(slo, name):
        c2 = json.loads(json.dumps(cur))
        c2['hub_mode'] = 'snapshot'
        c2['hub'] = dict(base)
        c2['hub'].pop('slo', None)
        if slo is not None:
            c2['hub']['slo'] = slo
        payload = json.dumps(c2, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
        out = os.path.join(tmpdir, f'slo-{name}.html')
        with open(out, 'w') as f:
            f.write(re.sub(r'(<script id="dash-data"[^>]*>)(.*?)(</script>)', lambda x: x.group(1) + payload + x.group(3), page, count=1, flags=re.S))
        dom = chrome(['--window-size=390,2400', '--dump-dom', f'file://{out}#health']).stdout
        a_ = dom.find('id="sys-hub"')
        return dom, (dom[a_:dom.find('</article>', a_)] if a_ >= 0 else '')

    dom, card = render({'tiles': tiles, 'at': 1, 'fresh': True}, 'full')
    ok('<div class="err"' not in dom and card, 'hub slo: health tab renders the hub card')
    ok('Service levels, 7 d: 4 of 6 met · 1 no data' in card, 'hub slo: met count over the scored tiles, no-data apart')
    ok(card.count('data-slo="') == 7, f'hub slo: one row per tile ({card.count("data-slo=")})')
    want = ['Tap → ack', 'First reply', 'Event → message', 'Vault answer', 'Job start lag', 'Reminder lateness', 'One trace per request',
            '0.1 s', '49 s', '119 s', '9.4 s', '3.1 s', '100 %', '&lt; 1 s', '&lt; 20 s', '≤ 10 s', '≤ 30 s', '≥ 95 %']
    ok(all(w in card for w in want), f'hub slo: labels, values and targets ({[w for w in want if w not in card]})')
    ok(card.count('class="chip warn">missed<') == 2 and card.count('class="chip ok">met<') == 4
       and card.count('class="chip unknown">no data<') == 1, 'hub slo: status chips (2 missed, 4 met, 1 no data)')
    ev = re.search(r'data-slo="event_message".*?</li>', card, re.S)
    ok(bool(ev) and 'class="v">–<' in ev.group(0), 'hub slo: a null value shows –, never 0')
    odd = tiles[:2] + [{'id': '<b>x</b>', 'value': 'abc', 'target': None, 'status': 'weird'}]
    dom, card = render({'tiles': odd, 'fresh': False}, 'stale')
    ok('(stale)' in card and '&lt;b&gt;x&lt;/b&gt;' in card and '<b>x</b>' not in card and 'class="v">–<' in card,
       'hub slo: stale block marked, an unknown id drawn as text, a non-number value as –')
    dom, card = render(None, 'none')
    ok(card and 'Service levels' not in card and 'data-slo' not in card, 'hub slo: no slo block, no section')
    # the key services sit under the container they run in (they are not containers of their own)
    part = lambda i, lab, kind, st='ok': {'id': i, 'label': lab, 'kind': kind, 'state': st}
    base = {'at': 1, 'gateway': {'tools': 103}, 'parts': [
        part('claude-hub', 'claude-hub', 'container'), part('claude-hub-tg', 'claude-hub-tg', 'container'),
        part('claude-hub-pg', 'claude-hub-pg', 'container'), part('tg-adapter', 'Telegram adapter', 'adapter'),
        part('hub-gateway', 'gateway', 'service'), part('zz-new', 'zznewsvc', 'service', 'warn'), part('domain-x', 'x', 'domain')]}
    dom, card = render(None, 'nest')
    rows_ = re.findall(r'<li (?:class="sub" )?data-kind="(\w+)">.*?class="w(?: mono)?">([^<]*)<', card)
    ok(rows_ == [('container', 'claude-hub'), ('service', 'gateway · 103 tools'), ('container', 'claude-hub-tg'),
                 ('service', 'Telegram adapter'), ('container', 'claude-hub-pg'), ('service', 'zznewsvc')],
       f'hub card: services nested under their container, an unknown one last ({rows_})')
    ok('3 containers · 3 key services · zznewsvc warn' in card, 'hub card: "N containers · M key services" + what is wrong')


def test_render(tmpdir):
    if not shutil.which('google-chrome'):
        ok(False, 'google-chrome available')
        return
    expect = {'health': ['Phone app &amp; gateway', 'Voice relay', 'Privacy vault', 'Web agent', 'Home autopilots', 'Server &amp; storage',
                         'Agents &amp; cost', '>Board<', 'Waiting on you', 'All checks', 'class="sys-hero'],
              'ci': ['id="ci-main"', 'id="ci-speed"', '>Lands<', '>Tool suites on main<', '>Cron, last 24 h<'],
              'agents': ['Main session', 'Working now', 'Ended in the last', 'Tap an agent to watch its session', 'class="ag-hero', 'Today $'],
              'today': ['Cost today', 'Agent runs today', 'Your messages today', '>Goals<', '>Work log<', '>Last night<', '>Today<',
                        '>Done<', 'Goal steps ticked', 'Questions answered', 'Reminders done', 'still waiting on you'],
              'usage': ['>Today<', 'Last 24 hours', 'Token usage', 'Cost per day', '>By type<', 'Where the tokens go', 'By model', 'Most used tools', '>Agent runs<', '>Latest<', 'How the estimate works'],
              'activity': ['Last 24 hours', 'Background agents', 'What woke me up', 'Activity per day', '>Reminders<'],
              'codebase': ['Lines of code + prompts', 'Scheduled jobs', 'Config snapshots', 'Lines over time', 'Counts over time', 'CLAUDE.md size',
                           'Code by language', 'Skills and subagents', '>What changed<', 'Around the clock'],
              'privacy': ['Where data is stored', 'Flows', 'Sent to Anthropic', 'Filtered by the vault', 'class="nodechips"'],
              'architecture': ['Architecture', 'How it works', 'watch its traffic live'] + ARCH_WORDS}
    gone = ['Cost per day by source', 'Estimated cost per day', 'Biggest agent runs', 'Latest agent runs', 'My task list', 'What I can do', '>Config history<',
            'Recent work', 'Recent changes', 'Changes to my setup', 'Tokens today', 'Features that run on their own', '🤖 Assistant', '📈 Growth', 'Health checks', '>Sources<', 'class="diagram', 'budget',
            'Jobs &amp; core', 'CI &amp; lands', '>System<']
    doms = {}
    for view, words in expect.items():
        r = chrome(['--window-size=390,2400', '--dump-dom', f'file://{PAGE}#{view}'])
        dom = doms[view] = r.stdout
        ok(all(w in dom for w in words), f'{view}: rendered text present ({[w for w in words if w not in dom]})')
        ok('<div class="err"' not in dom, f'{view}: no render error')
        sel = re.search(r'id="view-' + view + r'"[^>]*>', dom)
        ok(sel and 'hidden' not in sel.group(0), f'{view}: its section is the visible one')
    allv = doms['today']   # every tab is rendered into the DOM, so one dump holds all of them
    ok(allv.count('role="tab"') == 9 and allv.count('<section class="view"') == 9, 'nine tabs, nine sections')
    # retired cards: headings, tab labels and classes only (data such as a work-log line may quote an old card name)
    struct = ''.join('>' + re.sub(r'<[^>]+>', '', x[1]) + '<' for x in re.findall(r'<(h[1-6]|button)\b[^>]*>(.*?)</\1>', allv.split('<script')[0], re.S))
    struct += ' '.join(re.findall(r'class="[^"]*', allv.split('<script')[0]))
    ok(not [w for w in gone if w in struct], f'retired cards are gone ({[w for w in gone if w in struct]})')
    for old, new in (('overview', 'today'), ('system', 'health'), ('tokens', 'usage'), ('assistant', 'codebase'), ('growth', 'codebase'), ('data', 'privacy')):
        dom = chrome(['--window-size=390,1200', '--dump-dom', f'file://{PAGE}#{old}']).stdout
        sel = re.search(r'id="view-' + new + r'"[^>]*>', dom)
        ok(sel and 'hidden' not in sel.group(0) and '<div class="err"' not in dom, f'old hash #{old} opens {new}')
    dom = doms['usage']
    ok(dom.count('<path') > 20, 'token charts drew bars')
    ok('>avg $' in dom or 'no earlier days yet' in dom, 'cost chart shows the daily average line')
    cur = json.load(open(os.path.join(HOME, '.local', 'state', 'dash', 'data.json')))
    ok(doms['activity'].count('<circle') >= len(cur['activity']['timeline']['owner_messages']), 'timeline drew a dot per owner message')
    # every metric once: the numbers that used to disagree
    def view_of(dom, v):
        a_ = dom.find(f'id="view-{v}"')
        return dom[a_:dom.find('</section>', a_)]
    td_ = cur['tokens']['today']
    tday = view_of(allv, 'today')
    ok(f'>{td_["runs"]:,}<' in tday and f'{td_["subagent_runs"]:,} subagent' in tday, 'today: one "agent runs today" tile with its subagent split')
    ok(not any('subagents today' in view_of(allv, v) for v in ('today', 'codebase', 'activity')), 'no second "subagents today" number')
    g = cur.get('growth') or {}
    if g.get('days'):
        lt = g['days'][-1]['loc_total']
        cbv = view_of(allv, 'codebase')
        ok(cbv.count('>Lines of code') == 1 and '>Lines of code' not in tday and 'lines of code<' not in tday, 'one line-count tile, none on Today')
        ok(not g.get('langs') or sum(x['loc'] for x in g['langs']) == lt, 'code by language adds up to the Lines tile')
    br = cur['codebase']['brain']
    ok(view_of(allv, 'codebase').count(f'>{br["commits"]:,}<') == 1 and '>Config history<' not in allv, 'one config-snapshot count')
    hc = [c for c in cur['health']['checks'] if c['status'] != 'ok']
    sysv = view_of(allv, 'health')
    ok(all(c['name'] + '<' in sysv for c in hc), 'health: every non-OK health check (incl. the server) is listed')
    srv = [c for c in cur['health']['checks'] if c['name'] == 'Server (Unraid)']
    ok(not srv or srv[0]['value'] in sysv, 'health: the server\'s critical/warning counts are on the Server card')
    ok(not any(0x1F300 <= ord(ch) <= 0x1FAFF for ch in re.sub(r'<script.*?</script>', '', allv, flags=re.S)), 'no emoji on the rendered page')
    ok(sysv.count('In progress<') == 1 and allv.count('In progress<') == 1, 'to-do board shown once (Health)')
    hero = sysv[sysv.find('class="sys-hero'):sysv.find('id="sys-board"')]
    nw = len((cur['activity'].get('todo') or {}).get('waiting') or [])
    ok(('Waiting on you ' + str(nw) if nw else 'Nothing waiting on you') in hero, 'health: "Waiting on you N" in the hero, the Board right under it')
    ok(sysv.find('id="sys-board"') < sysv.find('class="sys-grid"'), 'health: the Board sits above the cards')
    ok('<button type="button" data-tab=' in hero or 'All systems normal' in hero, 'health: hero problems are buttons')
    usd = lambda x: '$' + format(round(x), ',') if x >= 100 else '$' + ('%.2f' % x)
    pair = 'Today ' + usd(cur['tokens']['today']['cost_usd'])
    ok(pair in hero and pair in view_of(allv, 'agents'), f'one cost answer on Health and Agents ({pair})')
    ok('Ended in the last' not in doms['agents'] or '<details class="ag-ended" open="">' in doms['agents'],
       'agents: the ended list is open by default')
    ok(view_of(allv, 'ci').count('Cost today') == 0 and view_of(allv, 'health').count('id="ci-cron"') == 0, 'ci: no cost, health: no cron heatmap')
    cb = view_of(allv, 'codebase')
    whens = re.findall(r'<ul class="chg">(.*?)</ul>', cb, re.S)
    ok(whens and cb.find('>What changed<') < cb.find('>Lines over time'), 'codebase: What changed comes first')
    ok('class="w live"' not in allv and 'w live-size' in view_of(allv, 'privacy'), 'privacy: store sizes do not use the sheet class')
    ok('class="tile wide headroom"' not in allv and 'disk-row' not in allv and 'disk_headroom' not in cur, 'no disk headroom tile (retired with trip-guard)')
    # work log: page copies with afternoon and morning data (mornings open on last night)
    page = open(PAGE).read()
    lnr = cur['activity'].get('lastnight') or {}

    def copy_with(name, hhmm):
        c2 = json.loads(json.dumps(cur))
        c2['generated_local'] = c2['generated_local'][:11] + hhmm
        payload = json.dumps(c2, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
        out = os.path.join(tmpdir, name)
        with open(out, 'w') as f:
            f.write(re.sub(r'(<script id="dash-data"[^>]*>)(.*?)(</script>)', lambda m: m.group(1) + payload + m.group(3), page, count=1, flags=re.S))
        return out
    mpage, apage = copy_with('morning.html', '07:30'), copy_with('afternoon.html', '15:30')
    dom = chrome(['--window-size=390,2400', '--dump-dom', f'file://{apage}#today']).stdout
    ok('>Today I did · ' in dom and '<div class="err"' not in dom, 'work log: afternoons open on today')
    dom = chrome(['--window-size=390,2400', '--dump-dom', f'file://{mpage}#today']).stdout
    if (lnr.get('tonight') or {}).get('count'):
        ok('>Last night · ' in dom and re.search(r'>Last \d nights</summary>', dom)
           and dom.count('<details class="table night"') == sum(1 for n in lnr.get('earlier', []) if n['count']),
           'work log: mornings open on last night, earlier nights collapsible')
    else:
        ok('>Today I did · ' in dom, 'work log: an empty night stays on today')
    # the artifact fragment inside a skeleton like the one claude.ai adds
    wrap = os.path.join(tmpdir, 'artifact.html')
    with open(wrap, 'w') as f:
        f.write('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, '
                'initial-scale=1, viewport-fit=cover"><style>[hidden]{display:none!important}body{margin:0}</style></head><body>'
                + open(FRAG).read() + '</body></html>')
    dom = chrome(['--window-size=390,2400', '--dump-dom', f'file://{wrap}#privacy']).stdout
    ok('Where data is stored' in dom and '<div class="err"' not in dom, 'artifact fragment renders')
    # phone screenshots (light, plus dark for two tabs)
    os.makedirs(REPORTS, exist_ok=True)
    for view in expect:
        out = os.path.join(REPORTS, f'phone-{view}.png')
        chrome(['--window-size=390,2200', f'--screenshot={out}', f'file://{PAGE}#{view}'])
        ok(os.path.exists(out) and os.path.getsize(out) > 20000, f'screenshot {view}')
    for name, dark in (('phone-today-morning.png', False), ('phone-today-morning-dark.png', True)):
        out = os.path.join(REPORTS, name)
        chrome(['--window-size=390,2600'] + (['--force-dark-mode'] if dark else []) + [f'--screenshot={out}', f'file://{mpage}#today'])
        ok(os.path.exists(out) and os.path.getsize(out) > 20000, f'screenshot {name}')
    dom = doms['privacy']
    live = cur['dataflow'].get('live') or {}
    ok(('Private lookups' in dom and 'Exact text released' in dom and 'Vault filter' in dom) if not live.get('error') else 'Live vault numbers unavailable' in dom,
       'data: live vault tiles (or the unavailable note) rendered')
    st = dom[dom.find('Where data is stored'):]
    ranks = [{'Critical': 0, 'High': 1, 'Medium': 2, 'Low': 3}[m] for m in re.findall(r'</span>(Critical|High|Medium|Low)</span>', st)]
    ok(len(ranks) == len(cur['dataflow']['stores']) and ranks == sorted(ranks), 'data: stores sorted by sensitivity, one chip each')
    ok(dom.count('<details class="store"') == len(cur['dataflow']['stores']), 'data: stores are compact expandable rows')
    used = {x for f in cur['dataflow']['flows'] for x in (f['from'], f['to'], f.get('via')) if x}
    ok(view_of(dom, 'privacy').count('<button type="button" class="g-') == len(used), 'privacy: one filter chip per place that has flows')
    dom = doms['codebase']
    g = cur.get('growth') or {}
    ok(g.get('days') and dom.count('<path') >= len(g.get('parts', [])) + 6 and 'class="empty">No growth data yet' not in dom, 'growth: stacked area + line charts drawn')
    ok(dom.count('class="mult"') == 4, 'growth: four count multiples')
    dom = chrome(['--window-size=390,2400', '--dump-dom', f'file://{PAGE}#architecture']).stdout
    sec = dom[dom.find('id="view-architecture"'):dom.find('<footer')]
    ok(re.match(r'id="view-architecture"[^>]*>\s*<div class="grid2 arch-grid">', sec)
       and sec.count('<svg class="arch arch-p"') == 1 and sec.count('<svg class="arch arch-l"') == 1,
       'architecture: both maps cloned into the view once')
    ok(dom.count('class="b') >= 60 and dom.count('marker-end=') >= 40, 'architecture: boxes and arrows drawn')
    for w in (390, 800, 1280):
        pr = arch_probe(tmpdir, w)
        ok(pr is not None, f'architecture probe ran at {w}px')
        if pr:
            want = 'arch arch-p' if w < 760 else 'arch arch-l'
            ok(pr['nvis'] == 1 and pr['vis'] == want, f'architecture: only the {want[10:]} map shown at {w}px ({pr["nvis"]} shown)')
            ok(pr['sw'] <= pr['iw'], f'architecture: no horizontal page scroll at {w}px')
            ok(not pr['overflow'], f'architecture: every label fits its box at {w}px ({pr["overflow"][:3]})')
            ok(pr['tabvis'], f'architecture: selected tab visible in the tab bar at {w}px')
            ok(0 < pr['svgw'] <= pr['panelw'], f'architecture: SVG fits its panel at {w}px')
            if w >= 1200:
                ok(pr['svgw'] >= 0.95 * pr['panelw'] and pr['notesbelow'], f'architecture: map spans the panel, notes under it at {w}px')
                ok(pr['lpx'] >= 12.5, f'architecture: box labels readable at {w}px ({pr["lpx"]:.1f}px)')
    for view in ('agents', 'today', 'usage', 'activity', 'codebase', 'privacy', 'architecture'):
        out = os.path.join(REPORTS, f'phone-{view}-dark.png')
        chrome(['--window-size=390,2200', '--force-dark-mode', f'--screenshot={out}', f'file://{PAGE}#{view}'])
        ok(os.path.exists(out) and os.path.getsize(out) > 20000, f'dark screenshot {view}')


SPLIT_PROBE = """<script>setTimeout(function () {
  var tabs = [].slice.call(document.querySelectorAll('.tab')), tops = {};
  tabs.forEach(function (t) { tops[Math.round(t.getBoundingClientRect().top)] = 1; });
  document.body.setAttribute('data-probe', JSON.stringify({ n: tabs.length, rows: Object.keys(tops).length,
    clipped: tabs.filter(function (t) { return t.scrollWidth > t.clientWidth + 1; }).map(function (t) { return t.textContent; }),
    sw: document.documentElement.scrollWidth, iw: innerWidth }));
}, 300);</script>"""


def test_tabs_split(tmpdir):
    """The Health/CI split release on page copies: all 9 tabs on two rows at phone widths (none clipped, no page scroll), the
    Privacy highlight's short points, the stale marker after 10 min, Agents' ended list open by default."""
    cur = json.load(open(os.path.join(HOME, '.local', 'state', 'dash', 'data.json')))
    page = open(PAGE).read()

    def copy(name, c2, extra=''):
        payload = json.dumps(c2, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
        out = os.path.join(tmpdir, f'split-{name}.html')
        p2 = re.sub(r'(<script id="dash-data"[^>]*>)(.*?)(</script>)', lambda x: x.group(1) + payload + x.group(3), page, count=1, flags=re.S)
        if extra:
            i = p2.rindex('</body>')
            p2 = p2[:i] + extra + p2[i:]
        with open(out, 'w') as f:
            f.write(p2)
        return out
    out = copy('tabs', cur, SPLIT_PROBE)
    for w in (360, 390, 430):
        dom = chrome([f'--window-size={w},900', '--dump-dom', f'file://{out}#ci'], budget=2000).stdout
        m = re.search(r'data-probe="([^"]*)"', dom)
        pr = json.loads(m.group(1).replace('&quot;', '"').replace('&amp;', '&')) if m else {}
        ok(pr.get('n') == 9 and pr.get('rows') == 2 and not pr.get('clipped') and pr.get('sw', 1) <= pr.get('iw', 0),
           f'tabs: all 9 on two rows at {w}px, none clipped, no page scroll ({pr})')
    c2 = json.loads(json.dumps(cur))
    hl = (c2.get('dataflow') or {}).get('highlights') or [{'level': 'medium', 'title': 'x'}]
    hl[0] = dict(hl[0], text='zzkeys short line', points=['zzpoint one', '<b>zzpoint</b> two', 7])
    c2.setdefault('dataflow', {})['highlights'] = hl
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
    c2['generated_at'] = old.isoformat(timespec='seconds')
    dom = chrome(['--window-size=390,2400', '--dump-dom', 'file://' + copy('privacy', c2) + '#privacy']).stdout
    first = dom[dom.find('class="highlights"'):]
    first = first[:first.find('</div>', first.find('zzkeys'))]
    ok('<p>zzkeys short line</p><ul><li>zzpoint one</li><li>&lt;b&gt;zzpoint&lt;/b&gt; two</li></ul>' in first,
       'privacy: a highlight shows its short points under the text (strings only, as text)')
    fr = re.search(r'<div class="fresh[^"]*" id="fresh">(.*?)</div>', dom)
    ok(fr and 'class="fresh stale"' in fr.group(0) and '20 min ago' in fr.group(1) and 'class="stale-mark">stale<' in fr.group(1),
       f'fresh: 20-min-old data is marked stale ({fr.group(0)[:200] if fr else None})')
    c2['generated_at'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=4)).isoformat(timespec='seconds')
    dom = chrome(['--window-size=390,1200', '--dump-dom', 'file://' + copy('fresh', c2) + '#health']).stdout
    fr = re.search(r'<div class="fresh[^"]*" id="fresh">(.*?)</div>', dom)
    ok(fr and 'stale' not in fr.group(0) and '4 min ago' in fr.group(1), 'fresh: 4-min-old data is not stale')


# ------------------------------------------------------------------------------------------- 11. self-refresh
REFRESH_PROBE = """<script>setTimeout(function () {
  var d = [].filter.call(document.querySelectorAll('#view-health details'), function (x) { return /Show every container/.test(x.textContent); })[0];
  if (d) d.open = true;
  window.scrollTo(0, 400);
  window.__y = window.scrollY;
}, 5000);
setTimeout(function () { document.body.setAttribute('data-probe', JSON.stringify({ y: window.scrollY, y0: window.__y })); }, 140000);</script>"""


def test_refresh(tmpdir):
    """The page refreshes itself: served over http from a local test server whose data.json changes on
    every request, under 150 s of virtual time it fetches data.json at load and every 60 s, redraws in place, keeps a
    fold he opened open and the scroll position; the timer and the back-in-view refresh are wired."""
    import http.server
    import threading
    page = open(PAGE).read()
    ok('var REFRESH_MS = 60 * 1000' in page and 'setInterval(function () { if (!document.hidden) refresh(false); }, REFRESH_MS);' in page
       and "document.addEventListener('visibilitychange', back);" in page and 'setInterval(updateFresh, FRESH_TICK_MS);' in page,
       'refresh: data.json every 60 s, at once when back in view, the age line ticks')
    ok(page.count('sseRetry(') >= 7 and 'Math.min(60000, 2000 * Math.pow(2, r.n++))' in page,
       'refresh: every live stream retries with backoff after the server closed it')
    cur = json.load(open(os.path.join(HOME, '.local', 'state', 'dash', 'data.json')))
    i = page.rindex('</body>')
    body = (page[:i] + REFRESH_PROBE + page[i:]).encode()
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            path = self.path.split('?')[0]
            if path in ('/', '/index.html'):
                data, ctype = body, 'text/html'
            elif path == '/data.json':
                hits.append(1)
                c2 = dict(cur, generated_at=(dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=len(hits))).isoformat(timespec='seconds'),
                          generated_local='zzrefresh-%d' % len(hits))
                data, ctype = json.dumps(c2).encode(), 'application/json'
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        dom = chrome(['--window-size=390,1400', '--dump-dom', f'http://127.0.0.1:{srv.server_address[1]}/#health'],
                     timeout=120, budget=150000).stdout
    finally:
        srv.shutdown()
    fr = re.search(r'id="fresh">(.*?)</div>', dom)
    ok(len(hits) >= 3 and fr and 'zzrefresh-%d' % len(hits) in fr.group(1),
       f'refresh: data.json fetched at load and every 60 s, the page shows the newest ({len(hits)} fetches, {fr.group(1)[:60] if fr else None})')
    ok(re.search(r'<details class="table" open="">\s*<summary>Show every container', dom) is not None,
       'refresh: a fold he opened stays open through the redraws')
    m = re.search(r'data-probe="([^"]*)"', dom)
    pr = json.loads(m.group(1).replace('&quot;', '"')) if m else {}
    ok(pr.get('y0') and abs(pr.get('y', 0) - pr['y0']) < 2, f'refresh: the scroll position stays ({pr})')
    ok('<div class="err"' not in dom, 'refresh: no render error')


RELOAD_PROBE = """<script>if (!sessionStorage.getItem('dash-reload')) setTimeout(function () {   // the first load: he opens a fold, scrolls
  var d = document.querySelector('#view-ci details'); if (d) d.open = true;
  window.scrollTo(0, 300);
}, 5000);
setTimeout(function () { var d = document.querySelector('#view-ci details');
  document.body.setAttribute('data-probe', JSON.stringify({ y: window.scrollY, hash: location.hash, fold: !!(d && d.open),
    guard: sessionStorage.getItem('dash-reload') })); }, 75000);</script>"""


def test_version_reload(tmpdir):
    """A deploy reaches an open tab: served over http, the page carries version vA; data.json says vA, then vB from the
    second fetch (the 60 s refresh). The tab reloads itself once (the server keeps serving the vA copy, as in a publish
    race: no second reload), comes back on its tab (#ci) with the fold he opened and his scroll position."""
    import http.server
    import threading
    page = re.sub(r'<meta name="dash-version" content="[^"]*">', '<meta name="dash-version" content="vAAAAAAAAAAAA">', open(PAGE).read())
    cur = json.load(open(os.path.join(HOME, '.local', 'state', 'dash', 'data.json')))
    i = page.rindex('</body>')
    body = (page[:i] + RELOAD_PROBE + page[i:]).encode()
    hits = {'page': 0, 'data': 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            path = self.path.split('?')[0]
            if path in ('/', '/index.html'):
                hits['page'] += 1
                data, ctype = body, 'text/html'
            elif path == '/data.json':
                hits['data'] += 1
                data, ctype = json.dumps(dict(cur, page_version='vAAAAAAAAAAAA' if hits['data'] < 2 else 'vBBBBBBBBBBBB')).encode(), 'application/json'
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        dom = chrome(['--window-size=390,1400', '--dump-dom', f'http://127.0.0.1:{srv.server_address[1]}/#ci'],
                     timeout=150, budget=150000).stdout
    finally:
        srv.shutdown()
    m = re.search(r'data-probe="([^"]*)"', dom)
    pr = json.loads(m.group(1).replace('&quot;', '"')) if m else {}
    ok(hits['page'] == 2 and hits['data'] >= 3, f'version reload: one reload after the new version, no loop ({hits})')
    ok(pr.get('hash') == '#ci' and pr.get('fold') and abs(pr.get('y', 0) - 300) < 2,
       f'version reload: back on its tab with the opened fold and the scroll position ({pr})')
    ok('"v":"vBBBBBBBBBBBB"' in (pr.get('guard') or ''), 'version reload: the once-per-version guard is set')
    ok('<div class="err"' not in dom, 'version reload: no render error')


ARCH_PROBE = """<script>(function () {
  var de = document.documentElement, out = [], shown = [].filter.call(document.querySelectorAll('svg.arch'), function (s) { return s.getBoundingClientRect().width > 0; });
  var svg = shown[0] || null;
  var boxes = svg ? [].slice.call(svg.querySelectorAll('rect.b, rect.grp, rect.z')) : [];
  if (svg) [].forEach.call(svg.querySelectorAll('text.l, text.s, text.m'), function (t) {
    var bb = t.getBBox(), x = parseFloat(t.getAttribute('x')), y = parseFloat(t.getAttribute('y')), host = null;
    boxes.forEach(function (r) {   // smallest box holding the label's anchor point
      var rx = +r.getAttribute('x'), ry = +r.getAttribute('y'), rw = +r.getAttribute('width'), rh = +r.getAttribute('height');
      if (x >= rx && x <= rx + rw && y - 4 >= ry && y <= ry + rh && (!host || rw * rh < host[2] * host[3])) host = [rx, ry, rw, rh];
    });
    if (!host || bb.x < host[0] + 1 || bb.x + bb.width > host[0] + host[2] - 1 || bb.y < host[1] || bb.y + bb.height > host[1] + host[3] + 1)
      out.push(t.textContent);
  });
  var tab = document.getElementById('tab-architecture'), bar = tab.parentNode, tr = tab.getBoundingClientRect(), br = bar.getBoundingClientRect();
  var panel = svg && svg.closest('.panel');
  document.body.setAttribute('data-probe', JSON.stringify({ sw: de.scrollWidth, iw: innerWidth, overflow: out,
    tabvis: tr.left >= br.left - 1 && tr.right <= br.right + 1,
    svgw: svg ? svg.getBoundingClientRect().width : 0, panelw: panel ? panel.clientWidth : 0,
    nvis: shown.length, vis: svg ? svg.getAttribute('class') : '',
    notesbelow: !!(panel && document.querySelector('.arch-notes') && document.querySelector('.arch-notes').getBoundingClientRect().top >= panel.getBoundingClientRect().bottom),
    lpx: svg ? parseFloat(getComputedStyle(svg.querySelector('text.l')).fontSize) * svg.getBoundingClientRect().width / svg.viewBox.baseVal.width : 0 }));
})();</script>"""


def arch_probe(tmpdir, width):
    """Architecture tab measured in a real layout: page width, labels inside their boxes, tab in view."""
    page = open(PAGE).read()
    i = page.rindex('</body>')
    probe = os.path.join(tmpdir, 'arch-probe.html')
    with open(probe, 'w') as f:
        f.write(page[:i] + ARCH_PROBE + page[i:])
    dom = chrome([f'--window-size={width},1400', '--dump-dom', f'file://{probe}#architecture']).stdout
    m = re.search(r'data-probe="([^"]*)"', dom)
    return json.loads(m.group(1).replace('&quot;', '"').replace('&amp;', '&')) if m else None



# ------------------------------------------------------------------------------------------- 7. live filters (probe)
LIVE_FAKE = """<script>(function () {
  var now = Date.now() / 1000, pad = new Array(60).join('filler text ');
  window.__evs = [
    {id: 1, t: now - 7200, edge: 'me→vault', from: 'me', to: 'vault', kind: 'vault_call', chip: 'ask', actor: 'main', meta: {cmd: 'ask', tool: 'Bash'}},
    {id: 2, t: now - 100, edge: 'cron→inbox', from: 'cron', to: 'inbox', kind: 'note', chip: 'agent-watch', actor: 'cron', text: 'agent stalled', meta: {}},
    {id: 3, t: now - 40, edge: 'me→anthropic', from: 'me', to: 'anthropic', kind: 'request', chip: 'request', actor: 'main', text: pad + ' the needle sits deep here', meta: {}},
    {id: 4, t: now - 30, edge: 'me→tools', from: 'tools', to: 'me', kind: 'tool_result', chip: 'error', actor: 'main', text: 'boom: needle failed', meta: {exit: 2}},
    {id: 5, t: now - 20, edge: 'me→tools', from: 'me', to: 'tools', kind: 'tool_call', chip: 'Bash', actor: 'main', text: 'ls -la', meta: {cmd: 'ls'}},
    {id: 6, t: now - 10, edge: 'telegram→me', from: 'telegram', to: 'me', kind: 'message', chip: 'text', actor: 'main', text: 'Can you check the Needle server?', meta: {}}
  ];
  window.__es = [];
  function ES(url) {
    var s = this;
    s.url = url; s.readyState = 1; s.l = {}; window.__es.push(s);
    setTimeout(function () { s.emit('hello', {}); window.__evs.forEach(function (e) { s.emit('ev', e); }); }, 20);
  }
  ES.prototype.addEventListener = function (t, fn) { (this.l[t] = this.l[t] || []).push(fn); };
  ES.prototype.close = function () { this.readyState = 2; this.closed = true; };
  ES.prototype.emit = function (t, d) { if (this.closed) return; (this.l[t] || []).forEach(function (fn) { fn({ data: JSON.stringify(d) }); }); };
  window.EventSource = ES;
})();</script>"""

LIVE_PROBE = """<script>(function () {
  var out = [];
  function P() { return document.querySelector('aside.live'); }
  function liveES() { return (window.__es || []).filter(function (x) { return x.url.indexOf('edge=') >= 0 && !x.closed; }).pop(); }
  function snap(name) {
    var p = P(), st = null, e = liveES();
    try { st = localStorage.getItem('dash-live-f:all'); } catch (err) { st = 'blocked'; }
    out.push({ name: name, open: !!(p && !p.hidden), title: p ? p.querySelector('h3').textContent : '',
      n: document.querySelectorAll('aside.live .live-ev').length, marks: document.querySelectorAll('aside.live mark.lf-hit').length,
      openRows: document.querySelectorAll('aside.live .live-ev.open').length, errRows: document.querySelectorAll('aside.live .live-ev.is-err').length,
      q: p ? p.querySelector('.lf-search input').value : '', hash: location.hash, es: e ? e.url : '', nes: (window.__es || []).length, store: st,
      count: p ? p.querySelector('.ls-count').textContent : '', status: p ? p.querySelector('.ls-text').textContent : '',
      none: !!document.querySelector('aside.live .lf-none'), pressed: p ? p.querySelector('.lf-err').getAttribute('aria-pressed') : '',
      win: p ? p.querySelector('select[data-f="win"]').value : '', grid: p ? !p.querySelector('.lf-grid').hidden : null,
      sheet: p ? Math.round(p.getBoundingClientRect().height) : 0, list: p ? Math.round(p.querySelector('.live-list').getBoundingClientRect().height) : 0,
      bar: p ? Math.round(p.querySelector('.live-filters').getBoundingClientRect().height) : 0, ih: window.innerHeight,
      sw: document.documentElement.scrollWidth, iw: window.innerWidth, pw: p ? p.scrollWidth - p.clientWidth : 0 });
  }
  function sel(name, v) { var s = P().querySelector('select[data-f="' + name + '"]'); s.value = v; s.dispatchEvent(new Event('change')); }
  function click(css) { P().querySelector(css).click(); }
  var steps = [
    function () { snap('boot'); click('.lf-err'); },
    function () { snap('errors'); click('.lf-clear'); },
    function () { snap('errors-noq'); click('.lf-err'); sel('kind', 'tool_call'); },
    function () { snap('kind'); sel('kind', ''); sel('dir', 'in'); },
    function () { snap('dir'); sel('dir', ''); sel('win', '300'); },
    function () { snap('win'); click('.live-head button:first-of-type'); },
    function () { liveES().emit('ev', { id: 7, t: Date.now() / 1000, edge: 'telegram→me', from: 'telegram', to: 'me', kind: 'message', chip: 'text', actor: 'main', text: 'late one', meta: {} });
      snap('paused'); click('.live-head button:first-of-type'); },
    function () { snap('resumed'); click('.live-head button[aria-label="Close"]'); },
    function () { snap('closed'); document.querySelector('.live-all').click(); },
    function () { snap('reopened'); sel('kind', 'vault_call'); },
    function () { snap('nomatch'); click('.lf-reset'); },
    function () { snap('reset'); click('.lf-more'); },
    function () { snap('toggled'); location.hash = '#architecture&live=main-anthropic&st=err'; },
    function () { snap('hashnav'); document.documentElement.setAttribute('data-probe', JSON.stringify(out)); }
  ];
  var i = 0;
  function next() { if (i < steps.length) { try { steps[i++](); } catch (err) { out.push({ name: 'error', msg: String(err) }); i = steps.length; document.documentElement.setAttribute('data-probe', JSON.stringify(out)); return; } setTimeout(next, 500); } }
  setTimeout(next, 1500);
})();</script>"""


def live_probe(tmpdir, width, height):
    """The filter UI driven in a real layout over http (fake EventSource that replays six events)."""
    import http.server
    import threading
    d = os.path.join(tmpdir, 'live-probe')
    os.makedirs(d, exist_ok=True)
    page = open(PAGE).read()
    page = page.replace('<body>', '<body>' + LIVE_FAKE, 1)
    i = page.rindex('</body>')
    with open(os.path.join(d, 'index.html'), 'w') as f:
        f.write(page[:i] + LIVE_PROBE + page[i:])
    shutil.copy(os.path.join(WWW, 'data.json'), os.path.join(d, 'data.json'))

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=d, **kw)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f'http://127.0.0.1:{srv.server_address[1]}/index.html#architecture&live=all&q=needle'
        dom = chrome([f'--window-size={width},{height}', '--dump-dom', url], budget=15000).stdout
    finally:
        srv.shutdown()
    m = re.search(r'data-probe="([^"]*)"', dom)
    return json.loads(m.group(1).replace('&quot;', '"').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')) if m else None


def test_live_filters(tmpdir):
    for width, height in ((390, 844), (1280, 900)):
        tag = f'live filters {width}px'
        r = live_probe(tmpdir, width, height)
        ok(bool(r) and r[-1].get('name') == 'hashnav', f'{tag}: probe ran all steps ({(r or [{}])[-1]})')
        if not r or r[-1].get('name') != 'hashnav':
            continue
        S = {x['name']: x for x in r}
        b = S['boot']
        ok(b['open'] and b['title'] == 'All traffic' and b['q'] == 'needle', f'{tag}: bookmarked hash opens the filtered all-traffic view')
        ok(b['n'] == 3 and b['marks'] >= 3 and b['count'] == '3 of 6 shown', f'{tag}: search keeps matching rows and highlights ({b["n"]}, {b["marks"]}, {b["count"]!r})')
        ok(b['openRows'] == 1, f'{tag}: a hit below the fold opens its row')
        ok(b['es'].startswith('live?edge=*') and 'q=' not in b['es'], f'{tag}: all traffic streams edge=*, the search stays client-side')
        e = S['errors']
        ok(e['n'] == 1 and e['errRows'] == 1 and e['pressed'] == 'true' and 'err=1' in e['es'], f'{tag}: errors only (client + flowd err=1)')
        ok('st=err' in e['hash'] and 'q=needle' in e['hash'] and '"st":"err"' in (e['store'] or ''), f'{tag}: filters in the hash and localStorage')
        ok(S['errors-noq']['n'] == 1 and S['errors-noq']['q'] == '' and 'q=' not in S['errors-noq']['hash'], f'{tag}: clear button empties the search')
        ok(S['kind']['n'] == 1 and 'kind=tool_call' in S['kind']['es'], f'{tag}: kind filter (client + flowd)')
        ok(S['dir']['n'] == 2 and 'dir=in' in S['dir']['es'], f'{tag}: direction filter')
        w = S['win']
        ok(w['n'] == 5 and 'since=' in w['es'] and 'win=300' in w['hash'], f'{tag}: time window drops the 2 h old event and asks flowd since=')
        ok(S['paused']['n'] == 5 and S['paused']['status'] == 'paused · 1 new', f'{tag}: pause holds new events ({S["paused"]["status"]!r})')
        ok(S['resumed']['n'] == 6, f'{tag}: resume shows the held event')
        ok(not S['closed']['open'] and S['closed']['hash'] == '#architecture', f'{tag}: close clears the hash')
        ro = S['reopened']
        ok(ro['open'] and ro['win'] == '300' and 'win=300' in ro['hash'] and ro['n'] == 5, f'{tag}: reopening restores the stored filters')
        ok(S['nomatch']['n'] == 0 and S['nomatch']['none'], f'{tag}: no match shows a reset hint')
        rs = S['reset']
        ok(rs['n'] == 6 and rs['hash'] == '#architecture&live=all' and rs['count'] == '6 events', f'{tag}: reset shows everything ({rs["hash"]!r})')
        ok(all(x['sw'] <= x['iw'] and x['pw'] <= 0 for x in r), f'{tag}: no horizontal scroll, page or panel')
        hn = S['hashnav']
        ok(hn['open'] and hn['title'] == 'Me ⇄ Anthropic API' and hn['pressed'] == 'true' and 'err=1' in hn['es'] and 'anthropic' in hn['es'],
           f'{tag}: a pasted bookmark (hashchange) opens that view with its filters')
        if width < 760:
            ok(not rs['grid'] and S['toggled']['grid'], f'{tag}: filter grid collapsed by default on phone, opens on tap')
            ok(abs(rs['sheet'] - 0.44 * rs['ih']) < 3 and rs['bar'] <= 56 and rs['list'] >= 150,
               f'{tag}: sheet 44vh, compact bar ({rs["bar"]} px), list {rs["list"]} px')
            ok(S['toggled']['sheet'] > rs['sheet'] and S['toggled']['list'] >= 120, f'{tag}: open filters make the sheet taller ({S["toggled"]["sheet"]} px)')
        else:
            ok(rs['grid'] and not S['toggled']['grid'], f'{tag}: filter grid open by default on desktop, collapses on click')
            ok(rs['sheet'] == rs['ih'] and rs['list'] >= 380, f'{tag}: full-height drawer, list {rs["list"]} px')


def main():
    m = load_module()
    tmp = tempfile.mkdtemp(prefix='dash-tests-')
    try:
        test_clean(m)
        test_assistant_helpers(m)
        test_goals(m)
        test_done_metrics(m)
        test_lastnight(m)
        test_clock_edges(m)
        test_hour_slots(m)
        test_fixture()
        test_gz_subagent()
        test_split()
        test_template(m)
        test_vault_live()
        test_sys_voice(m)
        test_growth()
        if not os.path.exists(os.environ.get('DASH_DATAFLOW', os.path.join(HOME, '.config', 'dash', 'dataflow.json'))):
            print('skip: real run, privacy sweep of the real data, page and render: no live install '
                  '(~/.config/dash/dataflow.json missing)')
            print(f'{PASSES} passed, {len(FAILS)} failed')
            sys.exit(1 if FAILS else 0)
        d = test_real(tmp)
        r = subprocess.run([DASH, '--inline'], capture_output=True, text=True, timeout=180,   # never the real vault/live Chrome
                           env=dict(os.environ, DASH_VAULT='/bin/false', DASH_BROWSER='/bin/false'))
        ok(r.returncode == 0, 'dash-data --inline exits 0')
        test_privacy(d)
        test_log_privacy(load_module(), json.load(open(os.path.join(HOME, '.local', 'state', 'dash', 'data.json'))))
        test_page()
        test_agents_tab(open(PAGE).read(), json.load(open(os.path.join(HOME, '.local', 'state', 'dash', 'data.json'))))
        if '--no-render' not in sys.argv:
            test_render(tmp)
            test_render_dst(tmp, m)
            test_hub_slo_render(tmp)
            test_ci_render(tmp)
            test_doing_render(tmp)
            test_tabs_split(tmp)
            test_refresh(tmp)
            test_version_reload(tmp)
            test_live_filters(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f'{PASSES} passed, {len(FAILS)} failed')
    sys.exit(1 if FAILS else 0)


if __name__ == '__main__':
    main()
