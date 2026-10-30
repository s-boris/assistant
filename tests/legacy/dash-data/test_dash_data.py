#!/usr/bin/env python3
"""Offline tests for ~/bin/dash-data as a whole run (the dash suite covers its helpers and the page).

Every case runs `dash-data` in a fresh temp HOME with a pinned clock (DASH_NOW), the synthetic fixture profile and
stubs for every outside call (vault, browser, whisper, agent-watch, dayline; tg-status and dash-growth
are simply absent). Nothing reaches the network, the real HOME or the real vault.
  1. empty HOME: exits 0, full shape, zeros and nulls, nothing invented
  2. missing / partial / corrupt inputs: truncated transcript lines, empty files, broken caches and state files
  3. normal shape: daily rows continuous up to today, 48 hourly slots, cost pace, cron, reminders, growth
  4. DST days: rows in their local day and hour (02:00 twice in autumn, none in spring), 23 h / 25 h day share
  5. month and year boundaries: gap-filled days across the turn, today's row, reminder 7-day window
  6. privacy: planted addresses, e-mails, phones, IBAN/AHV, tokens, amounts and names never reach data.json or the
     generated page (the dashboard is published)
  7. each section with its source missing, wrong-typed or partly malformed: empty value or only the good parts
  8. page_version: the template hash in data.json and the generated page's dash-version meta (open tabs reload on a deploy)
Run: tests/legacy/dash-data/run.sh      DASH_BIN=/path/to/copy tests a copy before installing.
"""
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo

REAL_HOME = os.path.expanduser('~')
DASH = os.environ.get('DASH_BIN', os.path.join(REAL_HOME, 'bin', 'dash-data'))
SECRETSCAN = os.path.join(REAL_HOME, 'bin', 'secretscan')
HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = next((p for p in (os.path.join(HERE, '..', 'profile-tests', 'fixtures', 'profile.json'),
                            os.path.join(HERE, '..', 'profile', 'fixtures', 'profile.json')) if os.path.isfile(p)), None)
Z = ZoneInfo('Europe/Zurich')
FAILS, PASSES = [], 0

# planted private values, assembled at run time so this file itself stays secretscan-clean
TOKEN = 'gh' + 'p_' + 'Q7w' * 12
PLANTED = {
    'email': 'anna.planted@example.org',
    'phone': '+41 79 555 66 77',
    'iban': 'CH93 0076 2011 6238 5295 7',
    'ahv': '756.1234.5678.97',
    'street': 'Plantedstrasse 12',
    'token': TOKEN,
    'ip': '.'.join(('203', '0', '113', '77')),   # documentation range, joined at run time (precheck's IP rule)
}
NAME = 'Zoltanina'                 # a first name from the (fixture) people note
AMOUNT = "CHF 1'234.50"


def ok(cond, name):
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(name)
        print('FAIL', name)


def w(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)
    os.chmod(path, mode)


def iso_z(t):
    return t.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def asst(mid, t, inp=100, out=50, model='claude-opus-5-5', text=''):
    content = [{'type': 'text', 'text': text}] if text else []
    return {'type': 'assistant', 'isSidechain': False, 'timestamp': iso_z(t), 'version': '9.9.9',
            'message': {'id': mid, 'model': model, 'role': 'assistant', 'content': content,
                        'usage': {'input_tokens': inp, 'output_tokens': out, 'cache_read_input_tokens': 0,
                                  'cache_creation_input_tokens': 0}}}


def user(t, kind='channel', text='hi'):
    return {'type': 'user', 'timestamp': iso_z(t), 'origin': {'kind': kind}, 'message': {'role': 'user', 'content': text}}


def jsonl(path, recs, tail=''):
    w(path, ''.join(json.dumps(r) + '\n' for r in recs) + tail)


STUB_OUT = "#!/bin/sh\ncat <<'EOF'\n%s\nEOF\n"


def make_home(root):
    """A bare temp HOME: secretscan linked in, outside calls stubbed. Returns (home, env)."""
    home = os.path.join(root, 'home')
    os.makedirs(os.path.join(home, 'bin'))
    os.symlink(SECRETSCAN, os.path.join(home, 'bin', 'secretscan'))
    for name in ('vault', 'browser', 'whisper'):
        w(os.path.join(root, 'stubs', name), '#!/bin/sh\nexit 1\n', 0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(('DASH_', 'DAYLINE_'))}
    env.update(HOME=home, DASH_VAULT=os.path.join(root, 'stubs', 'vault'), DASH_VAULT_TTL='0',
               DASH_BROWSER=os.path.join(root, 'stubs', 'browser'), DASH_WHISPER_CTL=os.path.join(root, 'stubs', 'whisper'),
               DASH_HUB_URL='http://127.0.0.1:9')   # H17: a closed loopback port, never the real hub
    if PROFILE:
        env['ASSISTANT_PROFILE'] = os.path.abspath(PROFILE)
    return home, env


def run(env, now, *args):
    e = dict(env, DASH_NOW=now.isoformat())
    return subprocess.run([DASH, *args], capture_output=True, text=True, env=e, timeout=150, stdin=subprocess.DEVNULL)


def data_of(r):
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


TOP_KEYS = {'schema', 'title', 'page_version', 'generated_at', 'generated_local', 'tz', 'now', 'tokens', 'activity', 'goals', 'context',
            'health', 'dataflow', 'codebase', 'growth', 'build_seconds'}
TOKEN_KEYS = {'totals', 'by_kind', 'by_model', 'today', 'cost_pace', 'last_24h', 'last_7d', 'daily', 'hourly',
              'top_runs', 'recent_runs', 'runs_total', 'tools_top', 'tool_calls_total', 'transcript_files'}


def day(d, date):
    return next((x for x in d['tokens']['daily'] if x['date'] == date), {'messages': None, 'turns': None})


# ------------------------------------------------------------------------------------------------ 1. empty HOME
def test_empty(root):
    home, env = make_home(root)
    now = dt.datetime(2026, 10, 18, 14, 5, tzinfo=Z)
    r = run(env, now, '--stdout')
    d = data_of(r)
    ok(r.returncode == 0 and isinstance(d, dict), f'empty: exits 0 with JSON ({r.stderr[-300:]})')
    if not isinstance(d, dict):
        return
    ok(TOP_KEYS <= set(d), f'empty: every top-level block present (missing {TOP_KEYS - set(d)})')
    t = d['tokens']
    ok(TOKEN_KEYS <= set(t), f'empty: every tokens field present (missing {TOKEN_KEYS - set(t)})')
    ok(d['generated_at'] == '2026-10-18T14:05:00+02:00' and d['generated_local'] == '2026-10-18 14:05' and d['tz'] == 'Europe/Zurich',
       'empty: DASH_NOW pins generated_at in the owner zone')
    ok(t['daily'] == [] and t['today'] is None and t['totals']['tokens'] == 0 and t['runs_total'] == 0
       and t['transcript_files'] == 0 and t['first'] is None, 'empty: no transcripts -> no days, zero totals')
    ok(len(t['hourly']) == 48 and t['hourly'][-1]['hour'] == '2026-10-18T14' and all(h['cost_usd'] == 0 for h in t['hourly']),
       'empty: 48 hourly slots ending at the pinned hour, all zero')
    ok(t['cost_pace']['avg_usd'] is None and t['cost_pace']['today_pct_of_avg'] is None
       and t['cost_pace']['day_elapsed_pct'] == round((14 * 60 + 5) / 1440 * 100, 1), 'empty: cost pace without history')
    a = d['activity']
    ok(a['todo'] == {'due': [], 'in_progress': [], 'waiting': [], 'ideas_count': 0} and a['changelog'] == [] and a['cron'] == []
       and a['reminders'] is None and a['agents'] == [] and a['dayline'] is None, 'empty: activity blocks empty, nothing invented')
    ok(d['growth'] is None, 'empty: no growth file -> growth null')
    ci = d.get('ci') or {}
    ok(ci.get('lands') == [] and (ci.get('queue') or {}).get('holder') is None and (ci.get('nightly') or {}).get('last') is None
       and (ci.get('runner') or {}).get('record') is None, 'empty: ci block without a deploy log: no lands, lock free, nothing invented')
    ok(a['timeline']['runs'] == [] and a['timeline']['owner_messages'] == [], 'empty: empty 24 h timeline')
    # the file-writing path: data.json + www/data.json; no template -> no page, --inline refuses
    r = run(env, now)
    st = os.path.join(home, '.local', 'state', 'dash')
    ok(r.returncode == 0 and os.path.isfile(os.path.join(st, 'data.json')) and os.path.isfile(os.path.join(st, 'www', 'data.json'))
       and not os.path.exists(os.path.join(st, 'www', 'index.html')), f'empty: writes data.json + www/data.json only ({r.stderr[-200:]})')
    r = run(env, now, '--inline')
    ok(r.returncode != 0 and 'cannot build dashboard.html' in (r.stderr + r.stdout), 'empty: --inline without a template fails clearly')


# ------------------------------------------------------------------------------------------- 2. partial / corrupt
def test_partial(root):
    home, env = make_home(root)
    now = dt.datetime(2026, 10, 18, 12, 0, tzinfo=Z)
    proj = os.path.join(home, '.claude', 'projects', '-home-node-workspace')
    jsonl(os.path.join(proj, 'S1.jsonl'), [asst('m1', now - dt.timedelta(hours=2)), user(now - dt.timedelta(hours=2))],
          tail='{"type": "assistant", "message": {"id": "m2", "usage": {"input_tok')   # being written: cut mid-line
    w(os.path.join(proj, 'S2.jsonl'), '')                                              # empty transcript
    w(os.path.join(proj, 'S3.jsonl'), 'not json at all\n\x00\x00garbage "assistant"\n')
    os.makedirs(os.path.join(proj, 'S1', 'subagents'))
    jsonl(os.path.join(proj, 'S1', 'subagents', 'agent-abc12345.jsonl'), [asst('s1', now - dt.timedelta(hours=1))])
    w(os.path.join(proj, 'S1', 'subagents', 'agent-abc12345.meta.json'), '{broken')            # broken meta
    st = os.path.join(home, '.local', 'state')
    w(os.path.join(st, 'dash', 'cache.json'), '{"v": 3, "files": [1, 2')                        # half-written cache
    w(os.path.join(st, 'dash', 'dayline.json'), '[]')
    w(os.path.join(st, 'context', 'today.json'), 'null')
    w(os.path.join(st, 'dash-growth.json'), json.dumps({'parts': 'scripts', 'days': {'2026-10-17': {}}, 'activity': [1]}))
    w(os.path.join(st, 'remind', 'reminders.json'), json.dumps([None, {'at': '2026-10-19 09:00', 'tag': 'bill:x', 'owner': True},
                                                                 'junk', {'tag': None}]))
    w(os.path.join(st, 'hb-gate.log'), 'garbage line\n\n')
    ws = os.path.join(home, 'workspace')
    w(os.path.join(ws, 'TODO.md'), 'no sections here\n- **orphan** — x\n## Due\n')
    w(os.path.join(ws, 'CHANGELOG.md'), '# Changelog\n- not a dated line\n- 2026-13-45 bad date\n- 2026-10-17 good line\n')
    w(os.path.join(ws, 'PROACTIVE.md'), '- 2026-10-18 03:00\n- \n')
    w(os.path.join(ws, 'GOALS.md'), '## \n- [x]\n')
    w(os.path.join(home, 'crontab'), '* * *\nFOO=bar\n# comment only\n*/5 * * * * \n7 3 * * * job-x >/dev/null\n')
    r = run(env, now, '--stdout')
    d = data_of(r)
    ok(r.returncode == 0 and isinstance(d, dict), f'partial: exits 0 with JSON despite broken inputs ({r.stderr[-400:]})')
    if not isinstance(d, dict):
        return
    t = d['tokens']
    ok(t['transcript_files'] == 4 and t['totals']['messages'] == 2, f'partial: the cut-off line is skipped, complete rows count '
       f'(files {t["transcript_files"]}, messages {t["totals"]["messages"]})')
    ok(t['by_kind'].get('subagent', {}).get('messages') == 1 and t['runs_total'] == 1,
       'partial: a subagent with a broken meta file still counts as one run')
    rm = d['activity']['reminders']
    ok(isinstance(rm, dict) and rm['total'] == 2 and rm['next_at'] == '2026-10-19 09:00' and rm['next_7d'] == 1,
       f'partial: reminders.json with null/junk entries -> only the real ones count ({rm})')
    ok(d['growth'] is None, 'partial: a malformed growth file (days not a list) -> growth null, no crash')
    ok(d['activity']['dayline'] is None and isinstance(d['context'], (dict, type(None))), 'partial: odd dayline/context files ignored')
    ok([c['job'] for c in d['activity']['cron']] == ['job-x'], f'partial: only the well-formed cron line is a job ({d["activity"]["cron"]})')
    ok(len(d['activity']['changelog']) <= 2 and 'not a dated line' not in json.dumps(d['activity']['changelog']),
       f'partial: changelog keeps dated lines only ({d["activity"]["changelog"]})')
    # the cache rebuilt cleanly: a second run reads it and gives the same numbers
    r2 = run(env, now, '--stdout')
    d2 = data_of(r2) or {}
    ok(r2.returncode == 0 and (d2.get('tokens') or {}).get('totals') == t['totals'], 'partial: rebuilt cache gives the same totals')


# ------------------------------------------------------------------------------------------- 3. normal shape
def test_shape(root):
    home, env = make_home(root)
    now = dt.datetime(2026, 10, 18, 15, 30, tzinfo=Z)
    proj = os.path.join(home, '.claude', 'projects', '-home-node-workspace')
    recs = []
    for k, days_back in enumerate((5, 3, 3, 0)):
        t = now - dt.timedelta(days=days_back, hours=1, minutes=k)
        recs += [user(t, 'human' if k == 0 else 'channel'), asst(f'a{k}', t + dt.timedelta(minutes=1), 1000, 200)]
    recs[2]['origin'] = {'kind': 'plugin', 'name': 'heartbeat'}   # k=1: a heartbeat beat
    jsonl(os.path.join(proj, 'S1.jsonl'), recs)
    jsonl(os.path.join(home, 'jobs', 'weekly', 'run.jsonl'), [asst('j1', now - dt.timedelta(hours=3), 10, 10, 'claude-sonnet-5-5')])
    w(os.path.join(home, 'workspace', 'TODO.md'), '# TODO\n## Due\n- **Pay the bill** — 20.10\n## In progress\n- **Build X** — running\n'
      '## Waiting on you\n- **Pick a date** — asked\n## Ideas\n- a\n- b\n')
    w(os.path.join(home, 'crontab'), '# Nightly snapshot of my notes: keeps history\n17 3 * * * $HOME/bin/brain-backup\n')
    w(os.path.join(home, '.local', 'state', 'dash-growth.json'), json.dumps({
        'version': 1, 'updated': '2026-10-18 15:00', 'parts': [{'key': 'scripts', 'label': 'Scripts (~/bin)'}],
        'days': [{'date': '2026-10-17', 'source': 'git', 'loc_total': 10, 'loc': {'scripts': 10}, 'counts': {'scripts': 2},
                  'langs': {'Python': 10}, 'files': {'scripts': 2}}],
        'activity': {'2026-10-16': {'heartbeats': 3, 'owner_msgs': 1, 'subagents': 0}}}))
    r = run(env, now, '--stdout')
    d = data_of(r)
    ok(r.returncode == 0 and isinstance(d, dict), f'shape: exits 0 ({r.stderr[-300:]})')
    if not isinstance(d, dict):
        return
    t = d['tokens']
    dates = [x['date'] for x in t['daily']]
    want = [(now.date() - dt.timedelta(days=k)).isoformat() for k in range(5, -1, -1)]
    ok(dates == want, f'shape: daily rows continuous from the first day to today ({dates})')
    ok(day(d, '2026-10-15')['messages'] == 2 and day(d, '2026-10-16')['messages'] == 0 and t['today']['date'] == '2026-10-18',
       'shape: two rows on the 15th, a gap day with zeros, today row')
    ok(all(set(x['tokens']) == {'main', 'subagent', 'headless'} for x in t['daily']), 'shape: per-kind tokens on every day')
    ok(t['by_kind']['headless']['messages'] == 1 and t['runs_total'] == 1, 'shape: the ~/jobs worker counts as a headless run')
    ok(day(d, '2026-10-15')['turns'] == {'heartbeat': 1, 'telegram': 1} and day(d, '2026-10-13')['turns'] == {'you (app/terminal)': 1},
       f'shape: turns per day by class ({day(d, "2026-10-15")["turns"]})')
    ok(len(t['hourly']) == 48 and len({h['hour'] for h in t['hourly']}) == 48, 'shape: 48 distinct hourly slots on a normal day')
    ok(t['cost_pace']['days'] == 5 and t['cost_pace']['day_elapsed_pct'] == round(15.5 / 24 * 100, 1), 'shape: cost pace over the 5 days before today')
    a = d['activity']
    ok(len(a['todo']['due']) == 1 and a['todo']['due'][0].startswith('Pay the bill'), f'shape: TODO due ({a["todo"]["due"]})')
    ok(a['todo']['ideas_count'] == 2 and len(a['todo']['waiting']) == 1 and len(a['todo']['in_progress']) == 1, 'shape: TODO sections counted')
    cr = a['cron']
    ok(len(cr) == 1 and cr[0]['schedule'] == '17 3 * * *' and cr[0]['enabled'] and 'brain-backup' in cr[0]['job']
       and cr[0]['description'].startswith('Nightly snapshot'), f'shape: crontab entry with schedule, label, description ({cr})')
    g = d['growth']
    ok(isinstance(g, dict) and [x['date'] for x in g['days']] == ['2026-10-17'] and g['langs'] == [{'lang': 'Python', 'loc': 10}],
       'shape: growth block from the dash-growth file')
    act = {x['date']: x for x in (g or {}).get('activity', [])}
    ok('2026-10-16' in act and act.get('2026-10-18', {}).get('owner_msgs') == 1, 'shape: growth activity = file days + this run\'s days')


# ------------------------------------------------------------------------------------------- 4. DST days
def dst_case(root, now, rows):
    home, env = make_home(root)
    recs = [asst(f'd{i}', t, 1000 * (i + 1), 0) for i, t in enumerate(rows)]
    jsonl(os.path.join(home, '.claude', 'projects', '-home-node-workspace', 'S1.jsonl'), recs)
    r = run(env, now, '--stdout')
    return r, data_of(r)


def test_dst(root):
    U = dt.timezone.utc
    # autumn: Sun 25.10.2026, 03:00 CEST -> 02:00 CET (01:00 UTC); the day is 25 h long
    rows = [dt.datetime(2026, 10, 24, 21, 59, tzinfo=U),    # 23:59 CEST on the 24th
            dt.datetime(2026, 10, 24, 22, 30, tzinfo=U),    # 00:30 CEST on the 25th
            dt.datetime(2026, 10, 25, 0, 30, tzinfo=U),     # 02:30 CEST (first 02:00 hour)
            dt.datetime(2026, 10, 25, 1, 30, tzinfo=U)]     # 02:30 CET (second 02:00 hour)
    now = dt.datetime(2026, 10, 25, 20, 0, tzinfo=Z)
    r, d = dst_case(os.path.join(root, 'autumn'), now, rows)
    ok(r.returncode == 0 and d, f'dst autumn: exits 0 ({r.stderr[-300:]})')
    if d:
        ok(day(d, '2026-10-24')['messages'] == 1 and day(d, '2026-10-25')['messages'] == 3,
           'dst autumn: rows land in their local day (00:30 CEST is the 25th)')
        labels = [h['label'] for h in d['tokens']['hourly'] if h['day'] == 'Sun']
        ok(labels.count('02:00') == 1 and labels.count('02:00′') == 1, f'dst autumn: 02:00 twice in the 48 h chart')
        two = [h['tokens']['main'] for h in d['tokens']['hourly'] if h['label'] in ('02:00', '02:00′') and h['day'] == 'Sun']
        ok(two == [3000, 4000], f'dst autumn: each 02:30 row in its own 02:00 slot ({two})')
        ok(d['tokens']['cost_pace']['day_elapsed_pct'] == round(21 / 25 * 100, 1), 'dst autumn: 20:00 = 21 of 25 hours elapsed')
        ok(d['generated_at'].endswith('+01:00'), 'dst autumn: generated_at in CET after the switch')
    # spring: Sun 28.03.2027, 02:00 CET -> 03:00 CEST (01:00 UTC); the day is 23 h long
    rows = [dt.datetime(2027, 3, 27, 22, 59, tzinfo=U),     # 23:59 CET on the 27th
            dt.datetime(2027, 3, 27, 23, 30, tzinfo=U),     # 00:30 CET on the 28th
            dt.datetime(2027, 3, 28, 0, 59, tzinfo=U),      # 01:59 CET
            dt.datetime(2027, 3, 28, 1, 0, tzinfo=U)]       # 03:00 CEST
    now = dt.datetime(2027, 3, 28, 12, 0, tzinfo=Z)
    r, d = dst_case(os.path.join(root, 'spring'), now, rows)
    ok(r.returncode == 0 and d, f'dst spring: exits 0 ({r.stderr[-300:]})')
    if d:
        ok(day(d, '2027-03-27')['messages'] == 1 and day(d, '2027-03-28')['messages'] == 3, 'dst spring: rows land in their local day')
        sun = [h for h in d['tokens']['hourly'] if h['day'] == 'Sun']
        ok('02:00' not in [h['label'] for h in sun] and [h['label'] for h in sun][:3] == ['00:00', '01:00', '03:00'],
           'dst spring: no 02:00 slot on the switch night')
        by = {h['label']: h['tokens']['main'] for h in sun}
        ok(by.get('01:00') == 3000 and by.get('03:00') == 4000, f'dst spring: 01:59 CET and 03:00 CEST in adjacent slots ({by})')
        ok(d['tokens']['cost_pace']['day_elapsed_pct'] == round(11 / 23 * 100, 1), 'dst spring: noon = 11 of 23 hours elapsed')


# ------------------------------------------------------------------------------------------- 5. month / year turn
def test_month(root):
    home, env = make_home(os.path.join(root, 'month'))
    now = dt.datetime(2026, 11, 1, 0, 30, tzinfo=Z)          # just after midnight on the 1st
    rows = [dt.datetime(2026, 10, 29, 12, 0, tzinfo=Z), dt.datetime(2026, 10, 31, 23, 50, tzinfo=Z),
            dt.datetime(2026, 11, 1, 0, 10, tzinfo=Z)]
    jsonl(os.path.join(home, '.claude', 'projects', '-home-node-workspace', 'S1.jsonl'),
          [asst(f'm{i}', t, 1000, 0) for i, t in enumerate(rows)])
    w(os.path.join(home, '.local', 'state', 'remind', 'reminders.json'), json.dumps([
        {'at': '2026-11-07 23:00', 'tag': 'bill:a', 'owner': True}, {'at': '2026-11-08 01:00', 'tag': 'deadline:b'},
        {'at': '2026-12-01 09:00'}]))
    r = run(env, now, '--stdout')
    d = data_of(r)
    ok(r.returncode == 0 and d, f'month: exits 0 ({r.stderr[-300:]})')
    if d:
        t = d['tokens']
        ok([x['date'] for x in t['daily']] == ['2026-10-29', '2026-10-30', '2026-10-31', '2026-11-01'],
           'month: days gap-filled across the month turn')
        ok(day(d, '2026-10-31')['messages'] == 1 and t['today']['date'] == '2026-11-01' and t['today']['messages'] == 1,
           'month: 23:50 stays on the 31st, 00:10 is today (the 1st)')
        ok(t['last_24h']['messages'] == 2 and t['last_7d']['messages'] == 3, 'month: 24 h / 7 d windows reach back over the turn')
        ok(t['cost_pace']['days'] == 3, 'month: cost pace averages the October days')
        rm = d['activity']['reminders']
        ok(rm['next_7d'] == 1 and rm['next_at'] == '2026-11-07 23:00' and rm['by_tag'].get('bill') == 1,
           f'month: reminder 7-day window crosses into the next week correctly ({rm})')
    home, env = make_home(os.path.join(root, 'year'))
    now = dt.datetime(2027, 1, 1, 9, 0, tzinfo=Z)
    jsonl(os.path.join(home, '.claude', 'projects', '-home-node-workspace', 'S1.jsonl'),
          [asst('y1', dt.datetime(2026, 12, 30, 22, 0, tzinfo=Z), 1, 1), asst('y2', dt.datetime(2027, 1, 1, 0, 0, tzinfo=Z), 1, 1)])
    r = run(env, now, '--stdout')
    d = data_of(r)
    ok(r.returncode == 0 and d and [x['date'] for x in d['tokens']['daily']] == ['2026-12-30', '2026-12-31', '2027-01-01'],
       'year: days run over New Year, 00:00 belongs to the new day')


# ------------------------------------------------------------------------------------------- 6. privacy
def test_privacy(root):
    home, env = make_home(root)
    now = dt.datetime(2026, 10, 18, 10, 0, tzinfo=Z)
    secret = ' '.join(PLANTED.values()) + f' {NAME} {AMOUNT}'
    proj = os.path.join(home, '.claude', 'projects', '-home-node-workspace')
    jsonl(os.path.join(proj, 'S1.jsonl'), [user(now - dt.timedelta(hours=1), 'channel', secret),
                                          asst('p1', now - dt.timedelta(minutes=50), text=secret)])
    os.makedirs(os.path.join(proj, 'S1', 'subagents'))
    jsonl(os.path.join(proj, 'S1', 'subagents', 'agent-feedbeef.jsonl'), [asst('p2', now - dt.timedelta(minutes=40), text=secret)])
    w(os.path.join(proj, 'S1', 'subagents', 'agent-feedbeef.meta.json'),
      json.dumps({'description': f'Mail {PLANTED["email"]} about {PLANTED["street"]}', 'agentType': 'general-purpose'}))
    ws = os.path.join(home, 'workspace')
    w(os.path.join(ws, 'TODO.md'), f'## Due\n- **Call {PLANTED["phone"]}** — today\n## In progress\n- **Pay {PLANTED["iban"]}** — x\n'
      f'## Waiting on you\n- **AHV {PLANTED["ahv"]} form** — asked\n')
    w(os.path.join(ws, 'CHANGELOG.md'), f'# Changelog\n- 2026-10-18 09:00 — {NAME}\'s gift sorted, {AMOUNT} for {PLANTED["email"]}\n'
      f'- 2026-10-17 — moved key {TOKEN} to {PLANTED["ip"]}\n')
    w(os.path.join(ws, 'PROACTIVE.md'), f'- 2026-10-18 02:00 done: checked {NAME}\'s plan ({AMOUNT}) at {PLANTED["street"]}\n')
    w(os.path.join(home, 'crontab'), f'# Ping {PLANTED["phone"]} and {PLANTED["email"]}: nightly\n5 4 * * * $HOME/bin/x\n')
    people = os.path.join(root, 'people-and-dates.md')
    w(people, f'# People\n- {NAME} Plantedname — friend, birthday 3 Mar\n')
    env['DASH_PEOPLE'] = people
    # outside calls that return private text: dayline and agent-watch stubs
    w(os.path.join(home, 'bin', 'dayline'), STUB_OUT % json.dumps({
        'date': '2026-10-18', 'changes': [], 'proactive': [{'time': '08:00', 'kind': 'done', 'text': f'{NAME}\'s {AMOUNT} at {PLANTED["street"]}'}]}), 0o755)
    w(os.path.join(home, 'bin', 'agent-watch'), STUB_OUT % (
        'ID        STATE    RUN    SILENT  LAST   OWNER TAG   DESCRIPTION\n'
        f'abcdef12  running  3m     1m      x      -           write to {PLANTED["email"]} re {PLANTED["iban"]}'), 0o755)
    page = os.path.join(root, 'page')
    w(os.path.join(page, 'index.html'), '<!doctype html><html><head><!--dash:head--><title>t</title><!--/dash:head--></head><body>'
      '<!--dash:body--><script id="dash-data" type="application/json" data-mode="fallback">{}</script><!--/dash:body--></body></html>\n')
    env['DASH_PAGE_DIR'] = page
    r = run(env, now, '--inline')
    ok(r.returncode == 0, f'privacy: dash-data --inline exits 0 ({r.stderr[-300:]})')
    st = os.path.join(home, '.local', 'state', 'dash')
    outs = {}
    for rel in ('data.json', 'www/data.json', 'www/index.html', 'www/dashboard.html'):
        p = os.path.join(st, rel)
        outs[rel] = open(p, encoding='utf-8').read() if os.path.isfile(p) else ''
    ok(all(outs.values()), f'privacy: all four outputs written ({[k for k, v in outs.items() if not v]})')
    d = json.loads(outs['data.json'] or '{}')
    ok(len(d.get('activity', {}).get('agents', [])) == 1 and len(d['activity']['todo']['due']) == 1
       and len(d['activity']['changelog']) == 2, 'privacy: the planted lines are there (masked), not dropped')
    for rel, text in outs.items():
        flat = re.sub(r'\s+', ' ', text.replace('\\u00a0', ' '))
        hits = [k for k, v in PLANTED.items() if v in text or v in flat or v.replace(' ', '') in flat.replace(' ', '')]
        ok(not hits, f'privacy: {rel} carries no planted {hits}')
        ok('Plantedname' not in text and f"{NAME}'s" not in text and NAME not in text, f'privacy: {rel} has no name from the people note')
        ok('1\'234' not in text and '1234.50' not in text, f'privacy: {rel} has no amount')
        ok(not re.search(r'\b\d{3}\s?\d{3}\s?\d{2}\s?\d{2}\b', text.replace(d.get('generated_at', '#'), '')),
           f'privacy: {rel} has no phone-shaped digit run')
    sc = subprocess.run([SECRETSCAN, '--stdin', '-q'], input=outs['www/index.html'], capture_output=True, text=True, timeout=60)
    ok(sc.returncode == 0, 'privacy: the generated page is secretscan-clean')
    # secretscan fails closed: a token the masks would miss -> nothing written (simulated with a scanner that always flags)
    w(os.path.join(root, 'flag-bin', 'secretscan'), '#!/bin/sh\ncat >/dev/null\necho "flagged: fake"\nexit 1\n', 0o755)
    os.remove(os.path.join(home, 'bin', 'secretscan'))
    os.symlink(os.path.join(root, 'flag-bin', 'secretscan'), os.path.join(home, 'bin', 'secretscan'))
    before = os.path.getmtime(os.path.join(st, 'data.json'))
    r = run(env, now + dt.timedelta(minutes=5))
    ok(r.returncode == 3 and os.path.getmtime(os.path.join(st, 'data.json')) == before,
       f'privacy: a secretscan hit writes nothing and exits 3 (rc {r.returncode})')


# ------------------------------------------------------------------------------- 7. each section's missing / odd source
def checks_of(d):
    return {c['name']: c for c in (d.get('health') or {}).get('checks', [])}


def test_sections(root):
    """Per data section: the source missing -> a clear empty value; malformed -> skipped or flagged, never a crash."""
    now = dt.datetime(2026, 10, 18, 14, 5, tzinfo=Z)
    # a. all sources missing: each section's empty value (the empty test covers tokens/activity/growth)
    home, env = make_home(os.path.join(root, 'a'))
    env['DASH_HUB'] = 'off'   # local checks only (no hub feed); test_hub covers feed mode
    d = data_of(run(env, now, '--stdout')) or {}
    g = d.get('goals') or {}
    ok(g.get('goals') == [] and (g.get('open'), g.get('done'), g.get('total')) == (0, 0, 0)
       and isinstance(g.get('done_metrics'), dict) and g['done_metrics'].get('reminders') is None,
       f'sections: no GOALS.md -> no goals, zero counts, done metrics without reminders ({g})')
    ok(d.get('context') is None, 'sections: no context snapshot -> context null')
    ok(d.get('dataflow') == {'error': 'data map missing or invalid: FileNotFoundError'}, f'sections: no data map -> error ({d.get("dataflow")})')
    ok(d.get('now') == {}, f'sections: no tg-status -> now {{}} ({d.get("now")})')
    a = d.get('activity') or {}
    ok(a.get('heartbeat') is None, 'sections: no hb-gate log -> heartbeat null')
    ln = a.get('lastnight') or {}
    ok(ln.get('tonight', {}).get('count') == 0 and ln.get('earlier') and all(n['count'] == 0 for n in ln['earlier'])
       and ln.get('untimed') == 0, f'sections: no PROACTIVE.md -> empty nights ({ln.get("tonight")})')
    c = checks_of(d)
    ok('Telegram channel' not in c and c.get('Privacy vault', {}).get('status') == 'critical'
       and c.get('Browser', {}).get('status') in ('warn', 'unknown') and c.get('Voice transcription', {}).get('status') == 'warn',
       f'sections: no channel check (retired) / failing vault, browser, whisper -> none / critical / warn ({ {k: v["status"] for k, v in c.items()} })')
    ok('Inbox backlog' not in c and c.get('Nightly backup', {}).get('status') == 'unknown',
       'sections: no inbox dir -> no backlog check; no backup log -> backup unknown')
    cb = d.get('codebase') or {}
    ok(cb.get('skills') == [] and cb.get('agents') == [] and cb.get('brain') is None
       and (cb.get('stats') or {}).get('tests', {}).get('suites') == 0, f'sections: empty HOME -> empty codebase block ({cb})')

    # b. malformed sources: wrong JSON types, garbage output, odd lines
    home, env = make_home(os.path.join(root, 'b'))
    env['DASH_HUB'] = 'off'   # local checks only (no hub feed); test_hub covers feed mode
    st = os.path.join(home, '.local', 'state')
    w(os.path.join(st, 'tg-status', 'state.json'), '[1, 2]')                       # valid JSON, not an object
    w(os.path.join(home, '.config', 'dash', 'dataflow.json'), '[]')
    w(os.path.join(st, 'context', 'today.json'), '["not", "a", "dict"]')
    w(os.path.join(st, 'remind', 'reminders.json'), '{"not": "a list"}')
    w(os.path.join(home, 'bin', 'agent-watch'), '#!/bin/sh\necho "no agents table here"\n', 0o755)
    r = run(env, now, '--stdout')
    d = data_of(r) or {}
    ok(r.returncode == 0 and d, f'sections: wrong-typed state files -> still exits 0 ({r.stderr[-300:]})')
    ok(d.get('now') == {}, f'sections: tg-status state that is a list -> now {{}} ({d.get("now")})')
    ok(str((d.get('dataflow') or {}).get('error', '')).startswith('data map missing or invalid'),
       f'sections: data map that is a list -> error ({d.get("dataflow")})')
    ok(d.get('context') is None, 'sections: context snapshot that is a list -> null')
    ok((d.get('activity') or {}).get('reminders') is None, 'sections: reminders.json that is an object -> null')
    ok((d.get('activity') or {}).get('agents') == [], 'sections: agent-watch without its table -> no agents')
    ok('Telegram channel' not in checks_of(d), 'sections: hub feed off -> no Telegram channel check (channel-check retired)')

    # c. partly good sources: only the good parts come through
    home, env = make_home(os.path.join(root, 'c'))
    env['DASH_HUB'] = 'off'   # local checks only (no hub feed); test_hub covers feed mode
    st = os.path.join(home, '.local', 'state')
    w(os.path.join(st, 'tg-status', 'state.json'), json.dumps({'mode': 'idle', 'since': 'junk'}))
    w(os.path.join(st, 'hb-gate.log'), '2026-10-18 10:00:01 wake todo\n2026-10-18 10:10:01 skip agent\n2026-10-18 10:20:01 bogus x\n'
                                        '2026-10-17 23:59:59 wake old\nshort\n')
    w(os.path.join(st, 'context', 'today.json'), json.dumps({'generated_at': '2026-10-18T13:00:00+02:00', 'hn': {'picks': [
        {'id': 'x'}, {'id': 7, 'title': 'Good one', 'points': '12', 'domain': 'example.org', 'tags': ['AI', 'other-tag']},
        'junk', {'title': 'no id'}]}, 'reminders': ['must not show'], 'location': {'zone': 'must not show'}}))
    w(os.path.join(home, 'bin', 'agent-watch'), '#!/bin/sh\ncat <<X\nID        STATE    RUN   SILENT LAST  OWNER TAG   DESCRIPTION\n'
      'abcdef1234 running  5m    1m     tool  -           Build the thing\n\nX\n', 0o755)
    w(os.path.join(root, 'c', 'stubs', 'vault'), '#!/bin/sh\n[ "$1" = status ] || exit 1\necho "vault up 3d"\necho "local LLM: healthy"\n'
      'echo "telegram: not logged in"\necho "alerts: 0"\n', 0o755)
    for i in range(7):
        w(os.path.join(home, 'inbox', f'note-{i}.md'), 'x')
    w(os.path.join(home, 'inbox', '.hidden'), 'x')
    w(os.path.join(home, 'workspace', 'GOALS.md'), '## 1. Build things (with notes)\n- [x] done one\n- [ ] blocked step (waiting on owner)\n'
      '- [ ] free step\n* [X] done two\n## Second goal\n- not a step\n')
    df = {'version': 3, 'routes': ['r'], 'viewbox': '0 0 1 1', 'nodes': [{'id': 'n1', 'label': 'N', 'x': 5, 'secret': 'y'}],
          'stores': [{'id': 's1', 'paths': [os.path.join(root, 'c', 'store')]},
                     {'id': 's2', 'paths': [os.path.join(root, 'c', 'store')], 'count_only': True},
                     {'id': 's3', 'paths': [os.path.join(root, 'c', 'nowhere')]}]}
    w(os.path.join(home, '.config', 'dash', 'dataflow.json'), json.dumps(df))
    w(os.path.join(root, 'c', 'store', 'a.bin'), 'x' * 100)
    w(os.path.join(root, 'c', 'store', 'sub', 'b.bin'), 'y' * 23)
    r = run(env, now, '--stdout')
    d = data_of(r) or {}
    ok(r.returncode == 0 and d, f'sections: partly good sources -> exits 0 ({r.stderr[-300:]})')
    ok(d.get('now') == {'mode': 'idle'}, f'sections: junk "since" dropped, mode kept ({d.get("now")})')
    a = d.get('activity') or {}
    ok(a.get('heartbeat') == {'wake_today': 1, 'skip_today': 1}, f'sections: hb-gate counts today\'s wake/skip only ({a.get("heartbeat")})')
    cx = d.get('context') or {}
    ok(cx.get('hn') == [{'id': 7, 'title': 'Good one', 'points': 12, 'domain': 'example.org', 'tags': ['AI']}]
       and cx.get('generated_local') == '2026-10-18 13:00' and isinstance(cx.get('age_min'), int),
       f'sections: context keeps only well-formed HN picks and known tags ({cx})')
    ok('must not show' not in json.dumps(d), 'sections: context reminders/location never copied')
    ag = a.get('agents') or []
    ok(len(ag) == 1 and ag[0]['id'] == 'abcdef12' and ag[0]['state'] == 'running' and ag[0]['owner_tag'] is None
       and ag[0]['description'] == 'Build the thing', f'sections: agent-watch table parsed, id cut to 8, "-" tag -> null ({ag})')
    c = checks_of(d)
    v = c.get('Privacy vault', {})
    ok(v.get('status') == 'ok' and 'up 3d' in v.get('value', '') and 'waiting on your api_id' in (v.get('detail') or ''),
       f'sections: healthy vault status parsed ({v})')
    ib = c.get('Inbox backlog', {})
    ok(ib.get('status') == 'warn' and ib.get('value', '').startswith('7 '), f'sections: 7 notes (hidden file ignored) -> backlog warn ({ib})')
    g = d.get('goals') or {}
    g1 = (g.get('goals') or [{}])[0]
    ok(len(g.get('goals') or []) == 2 and g1.get('title') == 'Build things' and (g1.get('done'), g1.get('open'), g1.get('blocked')) == (2, 2, 1)
       and g1.get('next') == 'free step' and g1.get('next_blocked') is False and g1.get('pct') == 50
       and (g.get('goals') or [{}, {}])[1].get('total') == 0,
       f'sections: GOALS.md counts, next step skips the blocked one ({g.get("goals")})')
    dfo = d.get('dataflow') or {}
    stores = {s['id']: s for s in dfo.get('stores', [])}
    ok(stores.get('s1', {}).get('live') == {'bytes': 123, 'files': 2} and stores.get('s2', {}).get('live') == {'files': 2}
       and stores.get('s3', {}).get('live') == {'bytes': 0, 'files': 0},
       f'sections: data map store sizes (count_only -> files only; missing path -> zero) ({stores})')
    ok(not ({'version', 'routes', 'viewbox'} & set(dfo)) and dfo.get('nodes') == [{'id': 'n1', 'label': 'N'}]
       and not any('paths' in s for s in stores.values()), f'sections: data map layout keys, node extras and paths stripped ({dfo.get("nodes")})')


# ------------------------------------------------------------------------------------------------ gym card
GYM_FIX = {
    'schema': 1, 'date': '2026-10-18', 'plan': {'name': 'Assistant plan', 'version': 1, 'days_per_week': 2, 'in_jefit': True},
    'week': {'start': '2026-10-12', 'sessions': 1, 'target': 2, 'status': 'gym'},
    'weeks': [{'start': '2026-08-%02d' % d, 'sessions': 1, 'status': 'gym'} for d in (10, 17, 24, 31)] +
             [{'start': s_, 'sessions': n, 'status': st} for s_, n, st in
              (('2026-09-21', 2, 'gym'), ('2026-09-28', 0, 'active'), ('2026-10-05', 2, 'gym'), ('2026-10-12', 1, 'bogus'))],
    'other_activity': {'since': '2026-09-01', 'this_week': {'shuffle': {'count': 1, 'hours': 1.0}, 'rave': {'count': 0, 'hours': 0.0}},
                       'total': {'shuffle': {'count': 4, 'hours': 4.0}, 'rave': {'count': 1, 'hours': 7.5}, 'yoga': {'count': 1}},
                       'weeks': [{'start': '2026-09-28', 'shuffle': {'count': 1, 'hours': 1.0}, 'rave': {'count': 1, 'hours': 7.5}},
                                 {'start': '2026-10-12', 'shuffle': {'count': 1, 'hours': 1.0}, 'rave': {'count': 0, 'hours': 0.0}}]},
    'streak_weeks': 2, 'longest_streak_weeks': 9, 'sessions_per_week_4w': 1.25, 'total_sessions': 40,
    'last_session': {'date': '2026-10-16', 'days_ago': 2, 'day': 'A'},
    'next': {'key': 'B', 'name': 'B · Deadlift + Press', 'est_min': 55, 'lead': 'Barbell Deadlift',
             'lead_sets': 3, 'lead_reps': [4, 6], 'lead_kg': 100.0},
    'lifts': [{'lift': 'squat', 'exercise': 'Barbell Squat', 'last': 120.5, 'last_date': '2026-10-16', 'change': 2.5, 'series': [['2026-10-16', 120.5]]},
              {'lift': 'bench', 'exercise': 'Bench ' + PLANTED['email'], 'last': 90.0, 'last_date': '2026-10-09', 'change': -1.0, 'series': []},
              {'lift': 'deadlift', 'exercise': 'Barbell Deadlift', 'last': 140.0, 'last_date': '2026-06-01', 'change': None, 'series': []},
              {'lift': 'curl', 'exercise': 'Curl', 'last': 30, 'last_date': '2026-10-16', 'change': 1}],
    'free_evenings': ['today', 'Mon', 'Someday'],
}


def gym_stub(home, out, code=0):
    w(os.path.join(home, 'bin', 'gym'), STUB_OUT % out + ('exit %d\n' % code if code else ''), 0o755)


CHROME_PROBE = re.compile(r'BLOCKED connect \[2001:4860:4860::8888\]:443 pid=\d+ cmd=\S*chrome --type=utility '
                          r'--utility-sub-type=network\.mojom\.NetworkService ')


def test_gym(root):
    """The Today tab's Gym card: gym missing/failing/malformed -> null; fields whitelisted, cleaned and range-checked;
    the page renders the card with the block and renders fine without it (old data)."""
    now = dt.datetime(2026, 10, 18, 14, 5, tzinfo=Z)
    home, env = make_home(os.path.join(root, 'a'))
    ok((data_of(run(env, now, '--stdout')) or {}).get('gym', 'x') is None, 'gym: no gym binary -> gym null')
    gym_stub(home, json.dumps(GYM_FIX))
    d = data_of(run(env, now, '--stdout')) or {}
    g = d.get('gym') or {}
    ok(g.get('week') == {'sessions': 1, 'target': 2, 'status': 'gym'} and len(g.get('weeks') or []) == 8
       and g['weeks'][-1] == {'start': '2026-10-12', 'sessions': 1, 'status': None, 'dance_h': 1.0}
       and g['weeks'][5] == {'start': '2026-09-28', 'sessions': 0, 'status': 'active', 'dance_h': 8.5}
       and g['weeks'][0]['dance_h'] is None,
       f'gym: this week vs plan + 8 weekly counts ({g.get("week")}, {len(g.get("weeks") or [])})')
    ok((g.get('streak_weeks'), g.get('longest_streak_weeks')) == (2, 9) and g.get('last_session') == {'date': '2026-10-16', 'days_ago': 2, 'day': 'A'},
       f'gym: streak and last session ({g.get("last_session")})')
    ok(g.get('next') == {'key': 'B', 'name': 'B · Deadlift + Press', 'lead': 'Barbell Deadlift', 'sets': 3, 'reps': [4, 6], 'kg': 100.0, 'est_min': 55},
       f'gym: next session with its lead exercise ({g.get("next")})')
    lifts = g.get('lifts') or []
    ok([x['lift'] for x in lifts] == ['squat', 'bench', 'deadlift'] and [x['change'] for x in lifts] == [2.5, -1.0, None]
       and lifts[0]['last'] == 120.5 and not any('series' in x for x in lifts),
       f'gym: the three main lifts only, last + 4-week change, no series shipped ({lifts})')
    ok(PLANTED['email'] not in json.dumps(d) and '[email]' in (lifts[1].get('exercise') or ''), 'gym: exercise names pass clean()')
    ok(g.get('free_evenings') == ['today', 'Mon'], f'gym: free evenings, unknown day names dropped ({g.get("free_evenings")})')
    ok(g.get('activity') == {'this_week': {'shuffle': {'count': 1, 'hours': 1.0}, 'rave': {'count': 0, 'hours': 0.0}},
                             'total': {'shuffle': {'count': 4, 'hours': 4.0}, 'rave': {'count': 1, 'hours': 7.5}}, 'weeks': 2},
       f'gym: dance this week + total, unknown kinds dropped ({g.get("activity")})')
    ok(set(g) == {'week', 'weeks', 'streak_weeks', 'longest_streak_weeks', 'last_session', 'next', 'lifts', 'free_evenings', 'activity'},
       f'gym: only the fields the page reads ({sorted(g)})')
    # bad fields -> null or empty, never a crash
    bad = dict(GYM_FIX, other_activity={'this_week': 'x', 'total': {'rave': {'count': 'many', 'hours': -2}}, 'weeks': 3}, weeks='x', lifts=None, week={'sessions': -3, 'target': 'two'}, last_session={'date': 'yesterday'},
               next={'name': None}, free_evenings='today', streak_weeks=True)
    gym_stub(home, json.dumps(bad))
    r = run(env, now, '--stdout')
    g = (data_of(r) or {}).get('gym') or {}
    ok(r.returncode == 0 and g.get('weeks') == [] and g.get('lifts') == [] and g.get('week') == {'sessions': None, 'target': None, 'status': None}
       and g.get('last_session') is None and g.get('next') is None and g.get('free_evenings') is None and g.get('streak_weeks') is None,
       f'gym: wrong-typed fields -> empty/null ({g})')
    for label, out, code in (('not JSON', 'oops', 0), ('a list', '[1, 2]', 0), ('exit 1', json.dumps(GYM_FIX), 1)):
        gym_stub(home, out, code)
        r = run(env, now, '--stdout')
        ok(r.returncode == 0 and (data_of(r) or {}).get('gym', 'x') is None, f'gym: {label} -> gym null')
    gym_stub(home, json.dumps(GYM_FIX))
    ok((data_of(run(dict(env, DASH_GYM='off'), now, '--stdout')) or {}).get('gym', 'x') is None, 'gym: DASH_GYM=off -> gym null')
    # render: the card with the block, no card (and no error) without it
    if not shutil.which('google-chrome'):
        ok(False, 'gym render: google-chrome available')
        return
    env['DASH_PAGE_DIR'] = os.environ.get('DASH_PAGE_DIR') or os.path.join(REAL_HOME, 'opt', 'dash')
    page = os.path.join(home, '.local', 'state', 'dash', 'www', 'index.html')
    # Chrome keeps the network guard (every connect still refused) but logs to its own file: its network service
    # always connects a UDP socket to Google's public DNS over IPv6 on :443 to learn whether IPv6 routes (no packet
    # goes out; no flag stops it in Chrome 156: the reachability override, resolver rules, a proxy, AsyncDns and DoH
    # off were tried). That one probe is expected; anything else in the log fails the run.
    glog = os.path.join(root, 'chrome-guard.log')
    cenv = dict(os.environ, NETGUARD_LOG=glog)
    for with_gym in (True, False):
        if not with_gym:
            os.remove(os.path.join(home, 'bin', 'gym'))
        r = run(env, now)
        prof = tempfile.mkdtemp(prefix='dash-gym-chrome-', dir=root)
        dom = subprocess.run(['google-chrome', '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--no-first-run',
                              '--disable-background-networking', '--disable-component-update', '--disable-sync',
                              '--disable-default-apps', '--disable-domain-reliability', '--no-pings', '--metrics-recording-only',
                              '--host-resolver-rules=MAP * ~NOTFOUND',
                              '--user-data-dir=' + prof, '--virtual-time-budget=4000', '--window-size=390,2400', '--dump-dom',
                              'file://' + page + '#today'], capture_output=True, text=True, timeout=90, env=cenv).stdout
        today = dom[dom.find('id="view-today"'):]
        today = today[:today.find('</section>')]
        if with_gym:
            want = ['>Gym<', '1 of 2 sessions + shuffle 1 h', 'shuffle 4× 4 h · rave 1× 7.5 h', 'Barbell Deadlift 3×4–6 @ 100 kg', 'Squat 120.5', '+2.5', '(01.06.)', 'today, Mon evenings',
                    'Gym sessions per week']
            ok(r.returncode == 0 and not [x for x in want if x not in today] and '<div class="err"' not in today,
               f'gym render: the card on Today with week, next, e1RM, evenings and the sparkline '
               f'(exit {r.returncode}, missing {[x for x in want if x not in today]}, err {"<div class=" + chr(34) + "err" in today})')
        else:
            ok(r.returncode == 0 and '>Gym<' not in dom and '<div class="err"' not in today and 'Agent runs today' in today,
               f'gym render: old data without the block -> no card, no error (exit {r.returncode}, {r.stderr[-200:]!r}, '
               f'gym {">Gym<" in dom}, today tab {"Agent runs today" in today}, dom {len(dom)})')
    try:
        with open(glog, errors='replace') as f:
            stray = [ln for ln in f.read().splitlines() if ln.strip() and not CHROME_PROBE.match(ln)]
    except FileNotFoundError:
        stray = []
    ok(not stray, f'gym render: Chrome reached for nothing but its IPv6 reachability probe ({stray[:2]})')


# ------------------------------------------------------------------------------------------- CI tab: cron heatmap
def test_cron_heat(root):
    """systems.cron.heat from cron-fail's run log: 24 local hours, a row per job that failed in them or fails now (its
    streak file), every other job by name with its runs per hour as base-36 digits; runs outside the window, bad lines and odd job names skipped; no run
    files -> null."""
    now = dt.datetime(2026, 10, 18, 14, 5, tzinfo=Z)
    home, env = make_home(os.path.join(root, 'a'))
    env['DASH_HUB'] = 'off'
    cf = os.path.join(home, '.local', 'state', 'cron-fail')
    os.makedirs(cf)
    ok((((data_of(run(env, now, '--stdout')) or {}).get('systems') or {}).get('cron') or {}).get('heat', 'x') is None,
       'cron heat: no run files -> null')
    t = now.timestamp()
    jsonl(os.path.join(cf, 'runs-20261018.jsonl'), [
        {'ts': t - 600, 'job': 'zzfail', 'rc': 1, 'ok': 0, 'ms': 10}, {'ts': t - 240, 'job': 'zzfail', 'rc': 0, 'ok': 1, 'ms': 10},
        {'ts': t - 200, 'job': 'zzok', 'rc': 0, 'ok': 1, 'ms': 5}, {'ts': t - 100, 'job': 'zz<b>x', 'rc': 1, 'ok': 0, 'ms': 5}],
          tail='{broken\n')
    jsonl(os.path.join(cf, 'runs-20261017.jsonl'), [{'ts': t - 25 * 3600, 'job': 'zzold', 'rc': 1, 'ok': 0, 'ms': 5},
                                                    {'ts': t - 22 * 3600, 'job': 'zzok', 'rc': 0, 'ok': 1, 'ms': 5}])
    w(os.path.join(cf, 'zzstreak.streak'), '3\n')
    hm = ((((data_of(run(env, now, '--stdout')) or {}).get('systems') or {}).get('cron') or {}).get('heat')) or {}
    jobs = hm.get('jobs') or []
    ok(hm.get('hours', [None])[0] == 15 and hm['hours'][-1] == 14 and len(hm['hours']) == 24, f'cron heat: 24 local hours ({hm.get("hours")})')
    ok([(j['job'], j['failing'], sum(j['runs']), sum(j['fail'])) for j in jobs] == [('zzfail', False, 2, 1), ('zzstreak', True, 0, 0)]
       and jobs[0]['fail'][22] == 1 and jobs[0]['runs'][23] == 1,
       f'cron heat: failed and failing jobs, per hour ({[(j["job"], j["failing"]) for j in jobs]})')
    o = hm.get('ok') or []
    ok(o == [['zzok', '01' + '0' * 21 + '1']] and 'others' not in hm and (hm.get('n_jobs'), hm.get('runs'), hm.get('failed')) == (3, 4, 1),
       f'cron heat: every other job by name, runs per hour in base 36, totals; old rows and odd names skipped ({o}, {hm.get("n_jobs")}, {hm.get("runs")})')
    ok(hm.get('recovered') == 1, f'cron heat: jobs that failed in 24 h and are not failing now ({hm.get("recovered")})')
    jsonl(os.path.join(cf, 'runs-20261018.jsonl'), [{'ts': t - 3000 + i, 'job': 'zzbusy', 'rc': 0, 'ok': 1, 'ms': 1} for i in range(40)])
    jsonl(os.path.join(cf, 'runs-20261017.jsonl'), [])
    hm = ((((data_of(run(env, now, '--stdout')) or {}).get('systems') or {}).get('cron') or {}).get('heat')) or {}
    ok(hm.get('ok') == [['zzbusy', '0' * 22 + 'z0']],
       f'cron heat: 35 or more runs in an hour read z ({hm.get("ok")})')
    ok('zz<b>' not in json.dumps(hm) and 'zzold' not in json.dumps(hm), 'cron heat: no markup, nothing older than 24 h')


# ------------------------------------------------------------------------------------------- growth: a clock ahead
SC_FIX = {'at': 1792328700.0, 'hour': 1792328400.0, 'metrics': [
    {'id': 'reply_p95', 'unit': 's', 'value': 30.0, 'base': 20.0, 'n': 9, 'status': 'ok', 'series': [{'t': 1, 'value': 1, 'n': 1}]},
    {'id': 'cron_fail', 'unit': 'share', 'value': 0.01, 'base': 0.02, 'n': 120, 'window_h': 6, 'status': 'ok',
     'series': [{'t': 1, 'value': 0.0, 'n': 30}, {'t': 2, 'value': None, 'n': 0}, 'x']},
    {'id': 'land_p95', 'unit': 's', 'value': 412.5, 'base': 300.0, 'n': 12, 'window_h': 12, 'status': 'high', 'note': 'zznote',
     'series': [{'t': i, 'value': 100.0 + i, 'n': 4} for i in range(60)]},
    {'id': 'land_fail', 'unit': 'share', 'value': True, 'base': 'x', 'n': 'many', 'status': '<b>bad</b>', 'series': None}]}


def test_scorecard(root):
    """The CI tab's speed tiles from `scorecard feed --json`: only land p95, failed lands and failed cron runs, in that
    order, numbers checked, at most 48 points, unknown status -> no-data; no feed (null), not JSON, exit 1, off -> null."""
    now = dt.datetime(2026, 10, 18, 14, 5, tzinfo=Z)
    home, env = make_home(os.path.join(root, 'a'))
    ok((data_of(run(env, now, '--stdout')) or {}).get('scorecard', 'x') is None, 'scorecard: no binary -> null')
    stub = os.path.join(home, 'bin', 'scorecard')
    w(stub, STUB_OUT % json.dumps(SC_FIX), 0o755)
    sc = (data_of(run(env, now, '--stdout')) or {}).get('scorecard') or {}
    ms = sc.get('metrics') or []
    ok([m['id'] for m in ms] == ['land_p95', 'land_fail', 'cron_fail'] and sc.get('at_local') == '15:05',
       f'scorecard: the three CI metrics in order, local time of the run ({[m["id"] for m in ms]}, {sc.get("at_local")})')
    lp = ms[0] if ms else {}
    ok(lp.get('value') == 412.5 and lp.get('base') == 300.0 and lp.get('n') == 12 and lp.get('status') == 'high'
       and lp.get('unit') == 's' and len(lp.get('series') or []) == 48 and lp['series'][0] == 112.0 and lp['series'][-1] == 159.0,
       f'scorecard: land p95 value, baseline, status, the last 48 points ({ {k: v for k, v in lp.items() if k != "series"} })')
    lf = ms[1] if len(ms) > 1 else {}
    ok(lf.get('value') is None and lf.get('base') is None and lf.get('n') == 0 and lf.get('status') == 'no-data' and lf.get('series') == [],
       f'scorecard: wrong-typed fields -> null / 0 / no-data ({lf})')
    ok((ms[2] if len(ms) > 2 else {}).get('series') == [0.0, None, None], 'scorecard: bad points -> null, never a made-up number')
    ok('zznote' not in json.dumps(sc) and '<b>' not in json.dumps(sc), 'scorecard: no notes or markup shipped')
    for label, out, code in (('no feed', 'null', 0), ('not JSON', 'oops', 0), ('exit 1', json.dumps(SC_FIX), 1)):
        w(stub, STUB_OUT % out + ('exit %d\n' % code if code else ''), 0o755)
        r = run(env, now, '--stdout')
        ok(r.returncode == 0 and (data_of(r) or {}).get('scorecard', 'x') is None, f'scorecard: {label} -> null')
    w(stub, STUB_OUT % json.dumps(SC_FIX), 0o755)
    ok((data_of(run(dict(env, DASH_SCORECARD='off'), now, '--stdout')) or {}).get('scorecard', 'x') is None,
       'scorecard: DASH_SCORECARD=off -> null')


def test_doing(root):
    """Health tab's "How I'm doing" (doing): the scorecard's reply p95 and cost per day (numbers checked, 48 points, USD
    unit), the ids over their bar now and how many metrics have a verdict; lands per local day for 8 days and today's red
    branches fixed by a later green land vs still red (counts only, no branch names); board items done per day from
    PROACTIVE.md's dated lines ('started:' lines skipped); parts missing -> empty / null; DASH_DOING=off -> null."""
    now = dt.datetime(2026, 10, 18, 14, 5, tzinfo=Z)
    home, env = make_home(os.path.join(root, 'a'))
    env['DASH_HUB'] = 'off'
    d = (data_of(run(env, now, '--stdout')) or {}).get('doing') or {}
    ok(d.get('metrics') == [] and d.get('over') == [] and d.get('lands') is None and d.get('done') is None,
       f'doing: nothing to read -> empty parts, nothing invented ({d})')
    fix = json.loads(json.dumps(SC_FIX))
    fix['metrics'] += [{'id': 'cost_day', 'unit': 'usd', 'value': 300.5, 'base': 100.0, 'n': 50, 'window_h': 24, 'status': 'regressed',
                        'series': [{'t': i, 'value': float(i), 'n': 1} for i in range(50)]},
                       {'id': '<b>zz</b>', 'unit': 's', 'value': 1, 'base': 1, 'n': 1, 'status': 'high', 'series': []}]
    w(os.path.join(home, 'bin', 'scorecard'), STUB_OUT % json.dumps(fix), 0o755)
    t = now.timestamp()
    land = lambda ts, b, ok_: {'op': 'land', 'ts': ts, 'end': ts, 'branch': b, 'ok': ok_}
    jsonl(os.path.join(home, '.local', 'state', 'assistant', 'deploys.jsonl'), [
        land(t - 3 * 3600, 'zzb1', False), land(t - 2 * 3600, 'zzb1', True), land(t - 3600, 'zzb2', False), land(t - 1800, 'zzb2', False),
        land(t - 86400, 'zzb3', True), land(t - 3 * 86400, 'zzb4', False), land(t - 9 * 86400, 'zzb5', True), land(t + 7200, 'zzb6', True),
        {'op': 'deploy', 'ts': t - 60, 'ok': False}], tail='{broken\n')
    w(os.path.join(home, 'workspace', 'PROACTIVE.md'), '# Proactive\n- 18.10 10:00 zzone\n- 18.10 11:00 started: zztwo\n'
      '- 2026-10-17 zzthree\n- 17.10 09:00 zzfour\n- 12.10 zzfive\n')
    d = (data_of(run(env, now, '--stdout')) or {}).get('doing') or {}
    ms = d.get('metrics') or []
    ok([m['id'] for m in ms] == ['reply_p95', 'cost_day'] and d.get('at_local') == '15:05',
       f'doing: reply p95 and cost per day, in that order ({[m["id"] for m in ms]})')
    cd = ms[1] if len(ms) > 1 else {}
    ok(cd.get('unit') == 'usd' and cd.get('value') == 300.5 and cd.get('base') == 100.0 and cd.get('status') == 'regressed'
       and len(cd.get('series') or []) == 48 and cd['series'][-1] == 49.0, f'doing: cost per day in USD, the last 48 points ({cd.get("value")})')
    ok(sorted((x['id'], x['status']) for x in d.get('over') or []) == [('cost_day', 'regressed'), ('land_p95', 'high')] and d.get('checked') == 4,
       f'doing: the metrics over their bar now, odd ids and states left out ({d.get("over")}, {d.get("checked")})')
    L = d.get('lands') or {}
    days = L.get('days') or []
    ok(len(days) == 8 and days[-1] == {'day': '2026-10-18', 'n': 4, 'green': 1} and days[-2] == {'day': '2026-10-17', 'n': 1, 'green': 1}
       and days[-4] == {'day': '2026-10-15', 'n': 1, 'green': 0} and days[0]['day'] == '2026-10-11' and sum(x['n'] for x in days) == 6,
       f'doing: lands per local day, 8 days, future and older rows left out ({days})')
    ok((L.get('red_today'), L.get('red_branches'), L.get('fixed'), L.get('open')) == (3, 2, 1, 1),
       f'doing: today\'s red branches, fixed by a later green land vs still red ({L})')
    dn = (d.get('done') or {}).get('days') or []
    ok([x['n'] for x in dn] == [0, 1, 0, 0, 0, 0, 2, 1] and dn[-1]['day'] == '2026-10-18',
       f'doing: items done per day from PROACTIVE.md, started lines skipped ({dn})')
    ok('zz' not in json.dumps(d), 'doing: counts only, no branch names, titles or markup')
    ok((data_of(run(dict(env, DASH_DOING='off'), now, '--stdout')) or {}).get('doing', 'x') is None, 'doing: DASH_DOING=off -> null')


def test_growth_future(root):
    """A growth file from a clock that ran ahead: dash-data refreshes it without --if-stale, into the file it reads
    (DASH_GROWTH_OUT), and never shows a point after today; a pinned clock never runs dash-growth on the live file."""
    home, env = make_home(root)
    gf, log = os.path.join(root, 'growth.json'), os.path.join(root, 'growth-calls.log')
    stub = os.path.join(root, 'stubs', 'dash-growth')
    w(stub, '#!/bin/sh\necho "$* |${DASH_GROWTH_OUT:-}" >> ' + log + '\n', 0o755)
    now = dt.datetime(2026, 10, 20, 12, 0, tzinfo=Z)
    pt = {'loc_total': 10, 'loc': {'scripts': 10}, 'counts': {'scripts': 1}, 'files': {'scripts': 1}, 'langs': {'Python': 10}}
    act = {'heartbeats': 1, 'owner_msgs': 1, 'subagents': 1}

    def write_growth(days, updated, epoch):
        with open(gf, 'w') as f:
            json.dump({'version': 1, 'updated': updated, 'updated_epoch': epoch, 'parts': [{'key': 'scripts', 'label': 'Scripts'}],
                       'days': [dict(pt, date=x, source='live' if x >= '2026-10-20' else 'git') for x in days],
                       'activity': {'2026-10-19': act, '2027-01-01': act}}, f)

    write_growth(['2026-10-19', '2026-10-20', '2026-10-21', '2027-03-28'], '2027-03-28 12:00', int(now.timestamp()) + 159 * 86400)
    e = dict(env, DASH_GROWTH=gf, DASH_GROWTH_BIN=stub)
    d = data_of(run(e, now, '--stdout'))
    g = (d or {}).get('growth') or {}
    calls = open(log).read().splitlines() if os.path.exists(log) else []
    ok(calls == [' |' + gf], f'growth ahead: dash-growth refreshed without --if-stale, into DASH_GROWTH ({calls})')
    ok([x['date'] for x in g.get('days', [])] == ['2026-10-19', '2026-10-20'] and g.get('updated') == ''
       and all(x['date'] <= '2026-10-20' for x in g.get('activity', [])), f'growth ahead: nothing after today on the page ({g.get("days")})')
    os.remove(log)
    write_growth(['2026-10-19', '2026-10-20'], '2026-10-20 11:30', int(now.timestamp()) - 1800)
    d = data_of(run(e, now, '--stdout'))
    calls = open(log).read().splitlines() if os.path.exists(log) else []
    ok(calls == ['--if-stale 55 |' + gf] and ((d or {}).get('growth') or {}).get('updated') == '2026-10-20 11:30',
       f'growth fresh: the hourly --if-stale refresh as before ({calls})')
    os.remove(log)
    run(dict(env, DASH_GROWTH_BIN=stub), now, '--stdout')
    ok(not os.path.exists(log), 'growth: a pinned clock (DASH_NOW) without DASH_GROWTH never runs dash-growth')


def test_hub(root):
    """H17: the hub feed's snapshot is embedded as the page's fallback; the mode switch (env or file) decides whether
    the page streams, and with the feed on dash-data no longer checks the Telegram channel itself."""
    import http.server
    import threading
    feed = {'seq': 7, 'at': 1.0, 'parts': [{'id': 'claude-hub', 'label': 'claude-hub', 'kind': 'container', 'state': 'ok',
                                           'detail': '', 'since': 1.0}], 'gateway': {'state': 'ok', 'calls_1h': 3}}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            b = json.dumps(feed if self.path == '/feed' else {}).encode()
            self.send_response(200 if self.path == '/feed' else 404)
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        home, env = make_home(os.path.join(root, 'hub'))
        env['DASH_HUB_URL'] = f'http://127.0.0.1:{srv.server_address[1]}'
        now = dt.datetime(2026, 10, 22, 14, 0, tzinfo=ZoneInfo('Europe/Zurich'))
        d = data_of(run(env, now, '--stdout'))
        ok(d['hub_mode'] == 'feed' and d['hub']['seq'] == 7, 'hub: feed mode embeds the snapshot')
        names = [c['name'] for c in d['health']['checks']]
        ok('Telegram channel' not in names, 'hub: feed mode drops the Telegram channel check')
        os.makedirs(os.path.join(home, '.config', 'dash'), exist_ok=True)
        with open(os.path.join(home, '.config', 'dash', 'hub-mode'), 'w') as f:
            f.write('snapshot\n')
        d = data_of(run(env, now, '--stdout'))
        ok(d['hub_mode'] == 'snapshot' and d['hub']['seq'] == 7, 'hub: the switch file selects snapshot mode')
        d = data_of(run(dict(env, DASH_HUB='off'), now, '--stdout'))
        ok(d['hub_mode'] == 'off' and d['hub'] is None, 'hub: off = no block')
        ok('Telegram channel' not in [c['name'] for c in d['health']['checks']], 'hub: off has no channel check either')
        d = data_of(run(dict(env, DASH_HUB_URL='http://127.0.0.1:9'), now, '--stdout'))
        ok(d['hub'] is None, 'hub: unreachable feed = no block, no failure')
    finally:
        srv.shutdown()


def test_agents(root):
    """Agents tab block (lib/agentfeed.py): a row per agent from agent-watch status --json + its transcript, texts only
    after agentfeed.clean; planted command, path, output, prompt and result text never in the data; DASH_AGENTS=off."""
    home, env = make_home(root)
    now = dt.datetime(2026, 10, 18, 14, 5, tzinfo=Z)
    sess, aid = 'sess-agents-0001', 'a0123456789abcdef'
    proj = os.path.join(home, '.claude', 'projects', '-home-node-workspace')
    t0 = now.timestamp() - 600
    T = lambda x: dt.datetime.fromtimestamp(x, Z)   # iso_z takes datetimes
    recs = [{'type': 'user', 'timestamp': iso_z(T(t0)), 'message': {'role': 'user', 'content': 'ZZPROMPT task text'}},
            {'type': 'assistant', 'timestamp': iso_z(T(t0 + 5)), 'message': {'id': 'm1', 'model': 'claude-opus-5-5', 'stop_reason': 'tool_use',
             'usage': {'input_tokens': 10, 'output_tokens': 5}, 'content': [{'type': 'tool_use', 'id': 't1', 'name': 'Bash',
             'input': {'command': 'cat /home/node/ZZCMD', 'description': 'Read the build config'}}]}},
            {'type': 'user', 'timestamp': iso_z(T(t0 + 6)), 'message': {'role': 'user', 'content': [
             {'type': 'tool_result', 'tool_use_id': 't1', 'content': 'ZZOUTPUT', 'is_error': False}]}},
            {'type': 'assistant', 'timestamp': iso_z(T(t0 + 9)), 'message': {'id': 'm2', 'model': 'claude-opus-5-5', 'stop_reason': 'end_turn',
             'usage': {'input_tokens': 10, 'output_tokens': 5}, 'content': [{'type': 'text', 'text': 'ZZRESULT done'}]}}]
    jsonl(os.path.join(proj, sess, 'subagents', f'agent-{aid}.jsonl'), recs)
    rows = {'now': now.timestamp(), 'hours': 6, 'agents': [{'id': aid, 'session': sess, 'status': 'done', 'kind': 'final',
            'start': t0, 'last': t0 + 9, 'description': 'Build the thing', 'type': 'general-purpose', 'effort': 'high',
            'depth': 1, 'parent': None}]}
    w(os.path.join(home, 'bin', 'agent-watch'), STUB_OUT % json.dumps(rows), 0o755)
    w(os.path.join(home, 'bin', 'traces'), STUB_OUT % json.dumps([{'id': aid, 'trigger': 'heartbeat', 'trigger_kind': 'idle'}]), 0o755)
    d = data_of(run(env, now, '--stdout'))
    A = (d or {}).get('agents') or {}
    row = (A.get('agents') or [{}])[0]
    ok(row.get('id') == aid and row.get('title') == 'Build the thing' and row.get('status') == 'done' and row.get('tools') == 1
       and row.get('trigger') == 'heartbeat · idle' and row.get('result_len') == len('ZZRESULT done') and row.get('tokens') == 30,
       f'agents: a row from agent-watch + the transcript + traces ({row})')
    blob = json.dumps(A)
    ok(not any(z in blob for z in ('ZZPROMPT', 'ZZCMD', 'ZZOUTPUT', 'ZZRESULT', '/home/node')),
       'agents: no prompt, command, path, output or result in the data')
    d = data_of(run(dict(env, DASH_AGENTS='off'), now, '--stdout'))
    ok(isinstance(d, dict) and d.get('agents') is None, 'agents: DASH_AGENTS=off -> null')


def test_page_version(root):
    """page_version: a short hash of the template in data.json (state copy, www copy, --stdout) and in the generated
    page's <meta name="dash-version">, so an open tab can tell a deploy; the template itself stays unstamped; a changed
    template gives a new version; no template -> null and no meta to stamp."""
    home, env = make_home(root)
    now = dt.datetime(2026, 10, 18, 10, 0, tzinfo=Z)
    page = os.path.join(root, 'page')
    tpl = ('<!doctype html><html><head><meta name="dash-version" content=""><!--dash:head--><title>t</title><!--/dash:head-->'
           '</head><body><!--dash:body--><script id="dash-data" type="application/json" data-mode="fallback">{}</script>'
           '<!--/dash:body--></body></html>\n')
    w(os.path.join(page, 'index.html'), tpl)
    env['DASH_PAGE_DIR'] = page
    want = 'v' + hashlib.sha256(tpl.encode()).hexdigest()[:12]
    r = run(env, now)
    st = os.path.join(home, '.local', 'state', 'dash')
    rd = lambda rel: open(os.path.join(st, rel), encoding='utf-8').read() if os.path.isfile(os.path.join(st, rel)) else ''
    d, dw, html_ = json.loads(rd('data.json') or '{}'), json.loads(rd('www/data.json') or '{}'), rd('www/index.html')
    mv = re.search(r'<meta name="dash-version" content="([^"]*)">', html_)
    emb = re.search(r'data-mode="fallback">(.*?)</script>', html_, re.S)
    ok(r.returncode == 0 and d.get('page_version') == dw.get('page_version') == want,
       f'page version: data.json and www/data.json carry the template hash ({d.get("page_version")}, {want}, rc {r.returncode})')
    ok(bool(mv) and mv.group(1) == want and emb and json.loads(emb.group(1)).get('page_version') == want,
       f'page version: the generated page and its inlined copy carry the same version ({mv.group(1) if mv else None})')
    ok(open(os.path.join(page, 'index.html'), encoding='utf-8').read() == tpl, 'page version: the template stays unstamped')
    ok((data_of(run(env, now, '--stdout')) or {}).get('page_version') == want, 'page version: --stdout carries it too')
    w(os.path.join(page, 'index.html'), tpl.replace('<title>t</title>', '<title>t2</title>'))
    run(env, now + dt.timedelta(minutes=5))
    v2 = json.loads(rd('www/data.json') or '{}').get('page_version')
    mv2 = re.search(r'<meta name="dash-version" content="([^"]*)">', rd('www/index.html'))
    ok(v2 and v2 != want and re.fullmatch(r'v[0-9a-f]{12}', v2) and mv2 and mv2.group(1) == v2,
       f'page version: a changed template gives a new version, page and data together ({v2})')
    os.remove(os.path.join(page, 'index.html'))
    r = run(env, now + dt.timedelta(minutes=10))
    ok(r.returncode == 0 and 'page_version' in json.loads(rd('www/data.json') or '{}')
       and json.loads(rd('www/data.json'))['page_version'] is None, 'page version: no template -> null, still exits 0')


def main():
    if not os.path.isfile(SECRETSCAN):
        print('skip: ~/bin/secretscan missing')
        return 0
    root = tempfile.mkdtemp(prefix='dash-data-tests-')
    try:
        for name, fn in (('empty', test_empty), ('partial', test_partial), ('shape', test_shape), ('dst', test_dst),
                         ('month', test_month), ('privacy', test_privacy),
                         ('sections', test_sections), ('gym', test_gym), ('scorecard', test_scorecard), ('cron-heat', test_cron_heat), ('doing', test_doing),
                         ('growth-future', test_growth_future), ('hub', test_hub),
                         ('agents', test_agents), ('page-version', test_page_version)):
            sub = os.path.join(root, name)
            os.makedirs(sub)
            fn(sub)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(f'{PASSES} passed, {len(FAILS)} failed')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
