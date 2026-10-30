#!/usr/bin/python3 -I
"""cifeed: the dashboard's CI tab: land queue, recent lands, nightly full suite, hub runner, headline, legacy suites
on main (with a suites x recent main runs grid), deploys, and a 24-hour timeline (lands per hour, deploy and main-run
markers).

  cifeed [--json]     print the snapshot (dash-data embeds it as `ci`, the 5-min fallback; flowd streams it live at
                      /live/ci while the Health or CI tab is open)

Sources, all read-only (never runs `assistant`, never takes or probes the deploy lock, never queues anything):
  ~/.local/state/assistant/deploys.jsonl   land rows (ok, why, land_cmd_start/land_start/end, lock_wait_secs,
                                           gate.ci_local_secs/ci_local_at/ci_local_steps, runner secs/created/started/
                                           ended/steps/failed steps, selection, tests, rerun, quarantined): a land's
                                           time is land_cmd_start -> end (land_start -> end is only merge + deploy),
                                           its timeline and step seconds; ci-nightly rows,
                                           legacy-main rows (ok, red suites, secs, skipped) and deploy rows (target, ok)
  ~/.local/state/assistant/legacy-main.json  each legacy suite's last result on main (ok, how, ts): the red ones
  ~/.local/state/assistant/deployed.json   the core release on this box (release, ts, files); hub.json the hub's
  ~/.local/state/assistant/deploy.lock     the holder's {pid, what, since}; held while that pid is a live assistant
  ~/.local/state/assistant/deploy.queue/   the waiters' tickets {pid, start, what, since} (live = pid + start match)
  /proc                                    `assistant land BRANCH` processes without lock or ticket yet (starting)
  ~/.local/state/assistant/deploy.queue/land-<pid>.phase   such a land's phase {pid, start, what, phase, since,
                                           land_since, rerun} (live = pid + start match): rebase, fail-fast, CI
                                           local / host + runner, runner, evals, waiting for lock; rerun = main moved
  ~/.local/state/assistant/ci-signed/      the hub runner's newest signed record (release, toolchain, python, ok)
  ~/.config/assistant/ci-gate              the land gate mode; ~/crontab: whether `assistant ci-nightly` is scheduled
  ~/opt/assistant (git, read-only)         the shown lands' `desc`, `body`, `commits`: the landed commit's subject and
                                           the first lines of its body (several commits: each subject, max 5), a red
                                           land's branch tip; cached by sha
Shown: branch names, step names, releases (12 hex), commit subjects and body lines (<= 300 chars), times (local ISO 8601), counts. Never test
ids, output or commands: every string is cut to a plain charset. Times in the owner's zone (profile locale.tz).
Env (tests): CIFEED_STATE, CIFEED_GATE, CIFEED_CRONTAB, CIFEED_PROC, CIFEED_NOW, CIFEED_TZ, CIFEED_REPO.
Tests: ~/opt/cifeed-tests/run.sh.
"""
import datetime as dt
import json
import os
import re
import statistics
import subprocess
import sys
import time

HOME = os.path.expanduser('~')
STATE = os.environ.get('CIFEED_STATE', os.path.join(HOME, '.local', 'state', 'assistant'))
GATE = os.environ.get('CIFEED_GATE', os.path.join(HOME, '.config', 'assistant', 'ci-gate'))
CRONTAB = os.environ.get('CIFEED_CRONTAB', os.path.join(HOME, 'crontab'))
PROC = os.environ.get('CIFEED_PROC', '/proc')
REPO = os.environ.get('CIFEED_REPO', os.path.join(HOME, 'opt', 'assistant'))


def _owner_tz():
    """CIFEED_TZ (tests), else the owner's zone (profile locale.tz via assistant_profile beside this file), else UTC."""
    try:
        from zoneinfo import ZoneInfo
        if os.environ.get('CIFEED_TZ'):
            return ZoneInfo(os.environ['CIFEED_TZ'])
        lib = os.path.dirname(os.path.realpath(__file__))
        if lib not in sys.path:
            sys.path.append(lib)
        import assistant_profile
        return assistant_profile.tz()
    except Exception:      # noqa: BLE001
        return dt.timezone.utc


TZ = _owner_tz()

LANDS = 15
GRID_RUNS = 12                   # the suites grid: the last 12 legacy-main runs x every suite
SPEEDUP = '696a6a7'              # the CI speed-up (28.10): lands after its land row are "after"
SPEEDUP_TS = 1793203895.0        # its commit time (28.10 17:11:35), when the row is not in the log
NIGHTLY_AT = (2, 10)             # cron `10 2 * * *  ... assistant ci-nightly`
WHY = {'ci': 'local CI', 'no signed record': 'runner (no signed green)', 'rebase conflict': 'rebase conflict',
       'eval subset': 'eval subset', 'known red on main': 'red on main', 'deploy': 'deploy'}
_tail = {'key': None, 'rows': []}


def word(v, n=48):
    """A plain token: letters, digits and . _ / + - only (branch, step, release); None when nothing is left."""
    s = re.sub(r'[^A-Za-z0-9._/+-]', '', str(v or ''))[:n]
    return s or None


def line(v, n=70):
    """A plain one-line text (a commit subject): letters, digits, spaces and plain punctuation only, cut to ~n chars on a
    word boundary with an ellipsis; None when nothing is left."""
    s = str(v or '').replace('`', '').replace('\u2192', '->').replace('\u00b7', ',').replace('\u2014', '-').replace('\u2013', '-')
    s = re.sub(r'\s+', ' ', re.sub(r"[^A-Za-z0-9 .,:;!?()'\"+/_&%#=<>*-]", ' ', s)).strip()
    if len(s) > n:
        cut = s[:n + 1].rsplit(' ', 1)[0] if ' ' in s[:n + 1] else s[:n]
        s = cut[:n].rstrip(' .,:;-') + '\u2026'
    return s or None


def num(v, nd=1):
    return round(float(v), nd) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None


def rows():
    """deploys.jsonl as dicts, re-read only when its size or mtime changed (flowd polls every 2 s)."""
    p = os.path.join(STATE, 'deploys.jsonl')
    try:
        s = os.stat(p)
    except OSError:
        return []
    key = (s.st_size, s.st_mtime_ns)
    if key != _tail['key']:
        out = []
        try:
            with open(p, encoding='utf-8') as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(d, dict) and d.get('op') in ('land', 'ci-nightly', 'legacy-main', 'deploy'):
                        out.append(d)
        except OSError:
            return _tail['rows']
        _tail.update(key=key, rows=out)
    return _tail['rows']


def _read_json(p):
    try:
        with open(p, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


_jcache = {}


def _read_json_cached(p):
    """_read_json, parsed again only when the file's size or mtime changed (flowd polls every 2 s)."""
    try:
        st = os.stat(p)
    except OSError:
        return None
    key = (st.st_size, st.st_mtime_ns)
    if _jcache.get(p, (None,))[0] != key:
        _jcache[p] = (key, _read_json(p))
    return _jcache[p][1]


def _stat(pid):
    """(state, start ticks) of a pid, None when gone."""
    try:
        with open(os.path.join(PROC, str(int(pid)), 'stat')) as f:
            s = f.read()
        x = s[s.rindex(')') + 2:].split()
        return x[0], x[19]
    except (OSError, ValueError, IndexError, TypeError):
        return None


def _argv(pid):
    try:
        with open(os.path.join(PROC, str(int(pid)), 'cmdline'), 'rb') as f:
            return [a.decode('utf-8', 'replace') for a in f.read().split(b'\0') if a]
    except (OSError, ValueError, TypeError):
        return []


def _listdir(d):
    try:
        return sorted(os.listdir(d))
    except OSError:
        return []


def _job(what):
    """'land BRANCH' / 'deploy' / 'rollback' / 'cron ...' -> (op, branch)."""
    parts = str(what or '').split()
    op = word(parts[0], 16) if parts else None
    return op, (word(parts[1]) if op == 'land' and len(parts) > 1 else None)


def _land_procs():
    """Live `assistant land BRANCH` processes: {pid: branch}."""
    out = {}
    try:
        names = [n for n in os.listdir(PROC) if n.isdigit()]
    except OSError:
        return out
    for n in names:
        ps = _stat(n)
        if not ps or ps[0] in ('Z', 'X'):
            continue
        a = _argv(n)
        for i, x in enumerate(a[:-1]):
            if os.path.basename(x) in ('assistant', 'assistant.py') and a[i + 1] == 'land':
                rest, skip = [], False
                for y in a[i + 2:]:     # land BRANCH [--adopt X]... [--override WHY]: flags with a value skip it
                    if skip:
                        skip = False
                    elif y in ('--adopt', '--override'):
                        skip = True
                    elif not y.startswith('-'):
                        rest.append(y)
                out[int(n)] = word(rest[0]) if rest else None
                break
    return out


def queue(now):
    procs = _land_procs()
    holder = None
    h = _read_json(os.path.join(STATE, 'deploy.lock'))
    if h and _stat(h.get('pid')) and _stat(h.get('pid'))[0] not in ('Z', 'X') and \
            any('assistant' in os.path.basename(x) for x in _argv(h.get('pid'))[:3]):
        op, br = _job(h.get('what'))
        holder = {'op': op, 'branch': br, 'since': num(h.get('since'), 0)}
    waiting, seen = [], {int(h['pid'])} if holder and str(h.get('pid', '')).isdigit() else set()
    qd = os.path.join(STATE, 'deploy.queue')
    try:
        tickets = sorted((f for f in os.listdir(qd) if re.fullmatch(r'\d+\.ticket', f)), key=lambda f: int(f[:-7]))
    except OSError:
        tickets = []
    for f in tickets:
        t = _read_json(os.path.join(qd, f))
        if not t:
            continue
        ps = _stat(t.get('pid'))
        if not ps or ps[0] in ('Z', 'X') or ps[1] != str(t.get('start')):
            continue
        op, br = _job(t.get('what'))
        waiting.append({'op': op, 'branch': br, 'since': num(t.get('since'), 0)})
        seen.add(int(t['pid']))
    known = {x['branch'] for x in waiting} | ({holder['branch']} if holder else set())
    phases = {}                            # branch -> the live land's phase record (assistant writes it, pre-lock)
    for f in _listdir(qd):
        m = re.fullmatch(r'land-(\d+)\.phase', f)
        t = _read_json(os.path.join(qd, f)) if m else None
        if not t or str(t.get('pid')) != m.group(1):
            continue
        ps = _stat(t.get('pid'))
        op, br = _job(t.get('what'))
        if ps and ps[0] not in ('Z', 'X') and ps[1] == str(t.get('start')) and op == 'land' and br \
                and t.get('phase') in PHASES:
            phases[br] = {'phase': t['phase'], 'phase_since': num(t.get('since'), 0), 'since': num(t.get('land_since'), 0),
                          'rerun': int(t['rerun']) if isinstance(t.get('rerun'), int) and 0 <= t['rerun'] < 100 else 0}
    starting, seen_b = [], set()
    for p, b in sorted(procs.items()):     # wrappers (cron-fail, sh -c) carry the same argv: one row per branch
        if p not in seen and b not in known and b not in seen_b:
            seen_b.add(b)
            starting.append({'op': 'land', 'branch': b, 'since': None, 'phase': None, 'phase_since': None, 'rerun': 0,
                             **phases.get(b, {})})
    return {'holder': holder, 'waiting': waiting[:12], 'starting': starting[:6]}


def _runs(r):
    """The land row's per-run test counts: (mode, files, of, tests) over its runs (the widest)."""
    t = r.get('tests') if isinstance(r.get('tests'), dict) else {}
    best = None
    for x in t.values():
        if isinstance(x, dict) and isinstance(x.get('files'), int):
            if best is None or x['files'] > best[0]:
                best = (x['files'], x.get('of') if isinstance(x.get('of'), int) else None,
                        x.get('tests') if isinstance(x.get('tests'), int) else None)
    return best


def _ids(d):
    """Distinct test ids across a rerun / quarantined map (the same id in both suites counts once)."""
    ids = set()
    for v in (d or {}).values() if isinstance(d, dict) else ():
        for i in (v.get('ids') if isinstance(v, dict) else v) or ():
            ids.add(str(i))
    return ids


def _steps(d):
    """{step: secs} -> [[step, secs], ...], slowest first (plain step names, numbers only)."""
    out = [[word(k, 24), num(v)] for k, v in (d.items() if isinstance(d, dict) else ()) if word(k, 24) and num(v) is not None]
    return sorted(out, key=lambda x: -x[1])[:16]


def _timeline(r, g, ru, end, cmd, start, ok):
    """The land as segments [kind, from_s, secs] from the command's start: local CI and the runner side by side
    (the runner after its queue), the deploy lock wait, merge + deploy; span = the whole land. Exact where the row has
    wall times (gate.ci_local_at, runner created/started/ended); else the CI from the start (the end of the time
    before the lock when main moved and the CI ran again). A red land: the CI lanes only."""
    L, R = num(g.get('ci_local_secs')), num(ru.get('secs'))
    total = end - (cmd or start) if ok and end and (cmd or start) and end >= (cmd or start) else None
    segs = []
    if total is None:
        if L:
            segs.append(['local', 0, L])
        if R:
            segs.append(['runner', 0, R])
        span = max(L or 0, R or 0)
        return {'span': num(span), 'segs': segs} if segs else None
    t0 = cmd or start
    merge = end - start if cmd and start and start >= cmd else None
    lock = max(0.0, num(r.get('lock_wait_secs')) or 0.0) if merge is not None else 0.0
    pre = total - (merge or 0) - lock      # rebase, checks, CI, evals (and an earlier pass when main moved)
    tries = (r.get('verify') or {}).get('tries') if isinstance(r.get('verify'), dict) else None
    base = 0.0 if not isinstance(tries, int) or tries <= 1 else max(0.0, pre - max(L or 0, R or 0))

    def seg(kind, a, s):
        a = min(max(0.0, a), total)
        s = min(max(0.0, s), total - a)
        if s > 0:
            segs.append([kind, num(a), num(s)])

    at = num(g.get('ci_local_at'), 3)
    if L:
        seg('local', at - t0 if at else base, L)
    cr, rs, re_ = (num(ru.get(k), 3) for k in ('created', 'started', 'ended'))
    if rs and re_ and re_ >= rs:
        if cr and rs > cr:
            seg('queue', cr - t0, rs - cr)
        seg('runner', rs - t0, re_ - rs)
    elif R:
        seg('runner', at - t0 if at else base, R)
    if merge is not None:
        seg('lock', pre, lock)
        seg('merge', total - merge, merge)
    return {'span': num(total), 'segs': segs}


def land(r):
    g = r.get('gate') if isinstance(r.get('gate'), dict) else {}
    ru = r.get('runner') if isinstance(r.get('runner'), dict) else g.get('runner') if isinstance(g.get('runner'), dict) else {}
    end, start = num(r.get('end') or r.get('ts'), 0), num(r.get('land_start'), 0)
    cmd = num(r.get('land_cmd_start'), 0)     # the whole `assistant land` (CI + land); before 29.10 land_start covered it
    t0 = cmd or start
    ok = bool(r.get('ok'))
    failed = []
    if not ok:
        why = str(r.get('why') or ('refused' if r.get('refused') else 'other'))
        if why == 'no signed record' and ru.get('failed'):
            failed = ['runner ' + (word(s, 32) or '?') for s in ru['failed'][:3]]
        elif why == 'known red on main' and isinstance(r.get('suites'), list):
            failed = ['suite ' + (word(s, 32) or '?') for s in r['suites'][:3]]
        elif isinstance(r.get('failed'), list) and r['failed']:
            failed = [word(s, 32) or '?' for s in r['failed'][:3]]
        else:
            failed = [WHY.get(why, word(why, 32) or 'other')]
    sel = r.get('selection') if r.get('selection') in ('impacted', 'full', 'stamp') else None
    cnt = _runs(r)
    rr = _ids(r.get('rerun'))
    rr_ok = all(v.get('ok') for v in r['rerun'].values() if isinstance(v, dict)) if isinstance(r.get('rerun'), dict) else None
    runner = None
    if ru:
        runner = 'signed' if ru.get('signed_green') else 'red' if ru.get('ok') is False else word(ru.get('state'), 12)
    return {
        't': end, 'branch': word(r.get('branch')), 'ok': ok, 'release': word(r.get('release'), 12),
        'total_s': num(end - t0, 0) if ok and end and t0 and end >= t0 else None,
        'local_s': num(g.get('ci_local_secs')), 'runner_s': num(ru.get('secs')), 'runner': runner,
        'merge_s': num(end - start) if ok and cmd and end and start and end >= start >= cmd else None,
        'wait_s': num(r.get('lock_wait_secs')) if ok else None,
        'queue_s': num(ru['started'] - ru['created']) if all(num(ru.get(k)) for k in ('created', 'started'))
        and ru['started'] >= ru['created'] else None,
        'tl': _timeline(r, g, ru, end, cmd, start, ok),
        'steps': {'runner': _steps(ru.get('steps')), 'local': _steps(g.get('ci_local_steps'))},
        'sel': sel, 'files': cnt[0] if cnt else None, 'of': cnt[1] if cnt else None, 'tests': cnt[2] if cnt else None,
        'reruns': len(rr), 'rerun_ok': rr_ok if rr else None, 'quarantined': len(_ids(r.get('quarantined'))),
        'failed': failed, 'enforced': g.get('effective') == 'enforce' if g else None,
    }


_dcache = {}
HEX = re.compile(r'^[0-9a-f]{7,40}$')


def _git(*args):
    try:
        p = subprocess.run(['git', '-C', REPO, *args], capture_output=True, text=True, timeout=5,
                           env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


TRAILER = re.compile(r'^(co-authored-by|claude-session|signed-off-by|reviewed-by|change-id):', re.I)
BODY_LINES, BODY_CHARS, SUBJECTS = 4, 300, 5


def _body(text):
    """A commit body's first paragraph (trailers skipped) as up to BODY_LINES plain lines, BODY_CHARS in all, cut on
    word boundaries; wrapped lines joined, list items ("- ", "* ") kept as their own lines."""
    for para in re.split(r'\n\s*\n', str(text or '').strip()):
        ls = []
        for raw in para.splitlines():
            t = raw.strip()
            if not t or TRAILER.match(t):
                continue
            if ls and not re.match(r'^([-*\u2022]|\d+[.)])\s', t):
                ls[-1] += ' ' + t
            else:
                ls.append(t)
        if not ls:
            continue
        out, left = [], BODY_CHARS
        for t in ls[:BODY_LINES]:
            if left < 24:
                break
            t = line(t, min(left, 160))
            if t:
                out.append(t)
                left -= len(t)
        return out
    return []


def desc(r):
    """What a land was about, from git (read-only): {desc, body, commits}. One commit (or no usable `from`): desc = its
    subject, body = its first paragraph (_body); several (from..sha, oldest first): desc = the first subject + "+N more",
    body = each subject (SUBJECTS, then "+N more"); a red land without a commit: its branch tip. Cached (a row never
    changes: by sha, a red row by branch + end), misses too; never raises."""
    none = {'desc': None, 'body': [], 'commits': None}
    sha = str(r.get('sha') or r.get('release') or '')
    sha = sha if HEX.match(sha) else ''
    frm = str(r.get('from') or '')
    frm = frm if HEX.match(frm) else ''
    br = word(r.get('branch'), 96)
    key = ('c', sha, frm) if sha else ('b', br, r.get('end') or r.get('ts')) if br and not br.startswith('-') else None
    if key is None:
        return none
    if key in _dcache:
        return _dcache[key]
    fmt = '--format=%s%x1f%b%x1e'
    if sha:
        cs = _commits(_git('log', '--reverse', '-n', '40', fmt, frm + '..' + sha)) if frm else []
        if not cs or len(cs) >= 40:     # 40: `from` not an ancestor (history rewritten): the commit's own message
            cs = _commits(_git('log', '-1', fmt, sha, '--'))[:1]
    else:
        cs = _commits(_git('log', '-1', fmt, 'refs/heads/' + br, '--'))[:1]
    out = none
    if len(cs) == 1:
        out = {'desc': line(cs[0][0], 120), 'body': _body(cs[0][1]), 'commits': 1}
    elif cs:
        first = line(cs[0][0], 90)
        subs = [x for x in (line(c[0], 100) for c in cs[:SUBJECTS]) if x]
        out = {'desc': first and first + ' +%d more' % (len(cs) - 1),
               'body': subs + (['+%d more' % (len(cs) - SUBJECTS)] if len(cs) > SUBJECTS else []), 'commits': len(cs)}
    if len(_dcache) > 500:
        _dcache.clear()
    _dcache[key] = out
    return out


def _commits(out):
    """git log %s%x1f%b%x1e records -> [(subject, body)]."""
    cs = []
    for rec in (out or '').split('\x1e'):
        subj, _, body = rec.strip('\n').partition('\x1f')
        if subj.strip():
            cs.append((subj, body))
    return cs


def _rate(ls):
    return {'n': len(ls), 'green': sum(1 for x in ls if x['ok']),
            'share': round(sum(1 for x in ls if x['ok']) / len(ls), 3) if ls else None,
            'median_s': num(statistics.median([x['total_s'] for x in ls if x['total_s']]), 0)
            if any(x['total_s'] for x in ls) else None}


def headline(lands_all, now, cut):
    midnight = dt.datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    tday = [x for x in lands_all if x['t'] and x['t'] >= midnight]
    week = [x for x in lands_all if x['t'] and x['t'] >= now - 7 * 86400]
    before = [x for x in lands_all if x['t'] and cut - 7 * 86400 <= x['t'] < cut]
    after = [x for x in lands_all if x['t'] and x['t'] > cut]     # the speed-up's own land still ran the old CI
    return {'today': _rate(tday), 'week': _rate(week), 'before': _rate(before), 'after': _rate(after),
            'speedup': {'commit': SPEEDUP, 'at': num(cut, 0)}}


def nightly(rs, now):
    last = next((r for r in reversed(rs) if r.get('op') == 'ci-nightly'), None)
    sched = False
    try:
        with open(CRONTAB, encoding='utf-8') as f:
            sched = any('assistant ci-nightly' in ln and not ln.lstrip().startswith('#') for ln in f)
    except OSError:
        pass
    n = dt.datetime.fromtimestamp(now, TZ)
    nxt = n.replace(hour=NIGHTLY_AT[0], minute=NIGHTLY_AT[1], second=0, microsecond=0)
    if nxt <= n:
        nxt = (n + dt.timedelta(days=1)).replace(hour=NIGHTLY_AT[0], minute=NIGHTLY_AT[1], second=0, microsecond=0)
    out = {'scheduled': sched, 'next': num(nxt.timestamp(), 0) if sched else None, 'last': None}
    if last:
        out['last'] = {'t': num(last.get('ts'), 0), 'ok': bool(last.get('ok')), 'secs': num(last.get('secs'), 0),
                       'head': word(last.get('head'), 12),
                       'red': [word(s, 32) or '?' for s in (last.get('red') or [])[:6] if isinstance(last.get('red'), list)]}
    return out


def runner(lands_all):
    d = os.path.join(STATE, 'ci-signed')
    best = None
    try:
        files = sorted((os.path.join(d, f) for f in os.listdir(d) if f.endswith('.json')), key=os.path.getmtime)[-8:]
    except OSError:
        files = []
    for p in files:
        a = _read_json(p) or {}
        rec = a.get('record') if isinstance(a.get('record'), dict) else None
        if rec and (best is None or (num(rec.get('signed_at')) or 0) >= (num(best[0].get('signed_at')) or 0)):
            best = (rec, bool(a.get('sig')))
    try:
        with open(GATE, encoding='utf-8') as f:
            mode = word(f.read().strip(), 16) or 'shadow'
    except OSError:
        mode = 'shadow'
    signed = sum(1 for x in lands_all if x['ok'] and x['runner'] == 'signed')
    out = {'gate': mode, 'signed_green_lands': signed, 'record': None}
    if best:
        rec, sig = best
        rn = rec.get('runner') if isinstance(rec.get('runner'), dict) else {}
        steps = [s for s in rec.get('steps') or [] if isinstance(s, dict)]
        py = next((s for s in steps if str(s.get('name', '')).startswith('pytest')), {})
        if isinstance(py.get('workers'), int):
            workers, wnote = py['workers'], None
        elif py.get('selection'):
            workers, wnote = None, 'xdist, count not recorded'
        else:
            workers, wnote = 1, 'serial (release before the CI speed-up)'
        out['record'] = {
            't': num(rec.get('signed_at') or rec.get('ended'), 0), 'ok': bool(rec.get('ok')), 'signed': sig,
            'release': word(rn.get('release') or rec.get('hub_release'), 12), 'toolchain': word(rn.get('toolchain'), 12),
            'python': word(rn.get('python'), 12), 'secs': num(rec.get('secs')), 'branch': word(rec.get('branch')),
            'failed': [word(s.get('name'), 32) or '?' for s in steps if not s.get('ok')][:4],
            'workers': workers, 'workers_note': wnote,
        }
    return out


def legacy(rs, now):
    """Legacy suites on main (`assistant legacy-main` after each land): how many, which are red now (legacy-main.json),
    the last run and a newer skip (high load), 7-day run counts. Suite names only, never their output."""
    d = _read_json_cached(os.path.join(STATE, 'legacy-main.json')) or {}
    su = d.get('suites') if isinstance(d.get('suites'), dict) else {}
    red = sorted(({'suite': word(k, 32) or '?', 't': num(v.get('ts'), 0), 'how': word(v.get('how'), 12)}
                  for k, v in su.items() if isinstance(v, dict) and v.get('ok') is False), key=lambda x: -(x['t'] or 0))
    runs = [r for r in rs if r.get('op') == 'legacy-main']
    done = [r for r in runs if not r.get('skipped')]
    week = [r for r in done if (num(r.get('ts'), 0) or 0) >= now - 7 * 86400]
    out = {'suites': len(su), 'red_n': len(red), 'red': red[:12], 'last': None, 'skipped': None,
           'runs_7d': len(week), 'green_7d': sum(1 for r in week if r.get('ok'))}
    if done:
        r = done[-1]
        out['last'] = {'t': num(r.get('ts'), 0), 'ok': bool(r.get('ok')), 'secs': num(r.get('secs'), 0),
                       'after': word(r.get('after')), 'head': word(r.get('head'), 12),
                       'red': [word(x, 32) or '?' for x in (r.get('red') if isinstance(r.get('red'), list) else [])[:6]]}
    if runs and runs[-1].get('skipped'):
        why = re.sub(r'[^A-Za-z0-9 ._-]', '', str(runs[-1]['skipped']))[:24].strip()
        out['skipped'] = {'t': num(runs[-1].get('ts'), 0), 'why': why or None}
    out['grid'] = grid(done, su)
    return out


def grid(done, su):
    """Every suite x the last GRID_RUNS legacy-main runs that ran (oldest first). Every run checks every suite, so a run's
    red list says each suite's result: `rows` = each suite red in any of these runs or red on main now (red now first,
    then the most red cells), cells 1 = red, 0 = green; `green` = the names of all other suites (green in every run:
    their cells are implied, which keeps the payload small), sorted. Suite names only."""
    rr = done[-GRID_RUNS:]
    reds = [{str(x) for x in r['red']} if isinstance(r.get('red'), list) else set() for r in rr]
    now_red = {str(k) for k, v in su.items() if isinstance(v, dict) and v.get('ok') is False}
    names = set(now_red).union(*reds)
    rows_ = sorted(({'suite': word(k, 32) or '?', 'red_now': k in now_red, 'cells': [1 if k in x else 0 for x in reds]} for k in names),
                   key=lambda x: (not x['red_now'], -sum(x['cells']), x['suite']))
    shown = {x['suite'] for x in rows_}
    return {'runs': [{'t': num(r.get('ts'), 0), 'ok': bool(r.get('ok')), 'head': word(r.get('head'), 12),
                      'after': word(r.get('after'))} for r in rr],
            'rows': rows_, 'green': sorted({word(k, 32) or '?' for k in su if k not in names} - shown)}


def deploys(rs, now):
    """What is deployed: the core release on this box (deployed.json, every green land), the hub release (hub.json)
    and the hub deploy rows (target, ok, release), newest first, with 7-day counts."""
    core = _read_json_cached(os.path.join(STATE, 'deployed.json')) or {}
    hub = _read_json_cached(os.path.join(STATE, 'hub.json')) or {}
    ds = [r for r in rs if r.get('op') == 'deploy']
    week = [r for r in ds if (num(r.get('ts'), 0) or 0) >= now - 7 * 86400]
    return {
        'core': {'release': word(core.get('release'), 12), 't': num(core.get('ts'), 0),
                 'files': len(core['files']) if isinstance(core.get('files'), dict) else None},
        'hub': {'release': word(hub.get('release'), 12)},
        'recent': [{'t': num(r.get('ts'), 0), 'target': word(r.get('target'), 16), 'ok': bool(r.get('ok')),
                    'release': word(r.get('release'), 12)} for r in reversed(ds[-8:])],
        'n_7d': len(week), 'failed_7d': sum(1 for r in week if not r.get('ok')),
    }


def timeline(rs, lands_all, now):
    """The last 24 hours by local hour, oldest first (the last one is this hour so far): green and red lands per hour,
    and markers (time, kind deploy | legacy | nightly, ok, deploy target) for hub deploys, legacy-main runs that ran
    and nightly full-suite runs. Every green land deploys core, so the green lands are the core deploys."""
    top = dt.datetime.fromtimestamp(now, TZ).replace(minute=0, second=0, microsecond=0).timestamp()
    t0 = top - 23 * 3600
    hours = [{'at': num(t0 + i * 3600, 0), 'green': 0, 'red': 0} for i in range(24)]
    for x in lands_all:
        if x['t'] and t0 <= x['t'] <= now + 60:
            hours[min(23, int((x['t'] - t0) // 3600))]['green' if x['ok'] else 'red'] += 1
    kinds = {'deploy': 'deploy', 'legacy-main': 'legacy', 'ci-nightly': 'nightly'}
    marks = []
    for r in rs:
        t = num(r.get('ts'), 0)
        if r.get('op') in kinds and not r.get('skipped') and t and t0 <= t <= now + 60:
            marks.append({'t': t, 'kind': kinds[r['op']], 'ok': bool(r.get('ok')),
                          'target': word(r.get('target'), 16) if r['op'] == 'deploy' else None})
    return {'at': num(t0, 0), 'hours': hours, 'marks': marks[-120:]}


TIME_KEYS = ('t', 'since', 'next', 'at', 'now', 'phase_since')
PHASES = ('rebase', 'fail-fast', 'CI local', 'CI host + runner', 'runner', 'evals', 'waiting for lock')


def iso(t):
    """Epoch -> local ISO 8601 with offset (the page parses it; no bare 10-digit epochs on the page)."""
    return dt.datetime.fromtimestamp(t, TZ).isoformat(timespec='seconds') if isinstance(t, (int, float)) else t


def _iso_tree(o):
    if isinstance(o, dict):
        return {k: iso(v) if k in TIME_KEYS else _iso_tree(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_iso_tree(x) for x in o]
    return o


def snapshot(now=None):
    now = float(now if now is not None else os.environ.get('CIFEED_NOW') or time.time())
    rs = rows()
    ls = [r for r in rs if r.get('op') == 'land']
    recent = [r for r in ls if (num(r.get('end') or r.get('ts'), 0) or 0) >= now - 15 * 86400]
    lands_all = [land(r) for r in recent]
    for x, r in zip(lands_all[-LANDS:], recent[-LANDS:]):     # only the shown lands ask git
        x.update(desc(r))
    cut = next((num(r.get('end') or r.get('ts'), 0) for r in ls
                if r.get('ok') and str(r.get('release') or '').startswith(SPEEDUP)), None) or SPEEDUP_TS
    return _iso_tree({
        'now': round(now), 'queue': queue(now), 'lands': list(reversed(lands_all[-LANDS:])),
        'headline': headline(lands_all, now, cut), 'nightly': nightly(rs, now), 'runner': runner(lands_all),
        'legacy': legacy(rs, now), 'deploys': deploys(rs, now), 'timeline': timeline(rs, lands_all, now),
    })


if __name__ == '__main__':
    s = snapshot()
    if '--json' in sys.argv[1:]:
        print(json.dumps(s, indent=1))
    else:
        q = s['queue']
        print('queue:', (q['holder'] or {}).get('branch') or (q['holder'] or {}).get('op') or 'free',
              '· waiting', len(q['waiting']), '· starting', len(q['starting']))
        h = s['headline']
        print('green today', h['today'], '· 7 d', h['week'])
        for x in s['lands']:
            print(' ', 'ok ' if x['ok'] else 'RED', x['branch'], x['total_s'], x['local_s'], x['runner_s'], x['sel'], x['failed'],
                  x.get('desc') or '')
