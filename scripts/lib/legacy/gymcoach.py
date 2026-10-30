"""gymcoach: the post-workout recap layer of ~/bin/gym (loaded by gym for `gym recap`).

  gym recap [--date DATE] [--json]
        short card of the last logged session (or the one on DATE): duration, hard sets, tonnage, top sets vs the
        previous time, PRs, plan day done/skipped, next-time weights, the week so far, the next session.
  gym recap --cron
        after `gym sync` (cron): a new session that looks the same on two polls in a row -> one cue note
        ~/inbox/gym-recap-DATE.md (no values: the session sends `gym recap`); first run, old backfills and
        already-recapped sessions stay silent. State: recap-state.json in the gym state dir.

Generic code: no personal values. Health data stays in the gym DB; notes carry no numbers.
Env: GYM_INBOX (note dir), USAGE=off.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))


def _usage(feature, kind='invoke', t0=None, outcome='ok', attrs=None):
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import usage
        ch = usage.caller_channel()
        usage.log(feature, kind, channel=ch, actor=usage.caller_actor(ch),
                  ms=(time.time() - t0) * 1000 if t0 else None, outcome=outcome, attrs=attrs)
    except Exception:
        pass


def short(name):
    """'Barbell Bench Press' -> 'Bench Press' (equipment words dropped, at least two words kept)."""
    w = name.split()
    while len(w) > 2 and w[0].lower() in ('barbell', 'dumbbell', 'machine', 'cable', 'weighted', 'smith', 'ez'):
        w = w[1:]
    return ' '.join(w)


def top_txt(g, top):
    w, r = top
    return f'{g.fmt_kg(w)}×{r}' if w else f'{r} reps'


# ----------------------------------------------------------------------------- per-lift history
def series(g, conn, end):
    """-> {name: {name, muscle, kind, equipment, weighted, exps: [{date, e1rm, top [w, reps], value, sets,
    reps, tonnage}]}} from the DB up to `end`; timed exercises skipped."""
    sessions, ex = g.load(conn)
    out = {}
    for s in sessions:
        if s['date'] > end.isoformat():
            continue
        for lg in s['logs']:
            e = ex[lg['ex']]
            if e['kind'] == 'timed':
                continue
            sets = [x for x in lg['sets'] if x['reps']]
            if not sets:
                continue
            rec = {'date': s['date'], 'sets': len(sets), 'reps': sum(x['reps'] for x in sets),
                   'tonnage': round(sum(x['w'] * x['reps'] for x in sets))}
            loaded = [x for x in sets if x['w'] > 0 and g.epley(x['w'], x['reps'])] if e['kind'] != 'bodyweight' \
                else []
            if loaded:
                top = max(loaded, key=lambda x: (g.epley(x['w'], x['reps']), x['w']))
                rec.update(e1rm=round(g.epley(top['w'], top['reps']), 1), top=[top['w'], top['reps']])
            else:
                top = max(sets, key=lambda x: (x['reps'], x['w']))
                rec.update(e1rm=None, top=[top['w'] if top['w'] > 0 else 0, top['reps']])
            d = out.setdefault(e['name'], {'name': e['name'], 'muscle': e['muscle'], 'kind': e['kind'],
                                           'equipment': e['equipment'], 'exps': []})
            d['exps'].append(rec)
    for d in out.values():
        d['weighted'] = sum(1 for x in d['exps'] if x['e1rm']) * 2 >= len(d['exps'])
        for x in d['exps']:
            x['value'] = x['e1rm'] if d['weighted'] else (x['top'][1] if not x['top'][0] else None)
    return out



# ----------------------------------------------------------------------------- post-workout recap
def fingerprint(s):
    sets = [x for lg in s['logs'] for x in lg['sets']]
    return f"{len(s['logs'])}:{len(sets)}:{sum(x['reps'] or 0 for x in sets)}:" \
           f"{round(sum(x['w'] or 0 for x in sets), 1)}:{s['duration_s'] or 0}"


def recap(g, conn, plan, end, date=None):
    sessions, ex = g.load(conn)
    sessions = [s for s in sessions if s['date'] <= end.isoformat()]
    if date:
        sessions = [s for s in sessions if s['date'] <= date.isoformat()]
        if not sessions or sessions[-1]['date'] != date.isoformat():
            return None
    if not sessions:
        return None
    s = sessions[-1]
    ser = series(g, conn, end)
    hard = ton = 0
    rows, prs = [], []
    for lg in s['logs']:
        e = ex[lg['ex']]
        g.mark_hard(lg, e['kind'])
        hard += sum(1 for x in lg['sets'] if x['hard'])
        ton += sum(x['w'] * x['reps'] for x in lg['sets'] if x['reps'] and x['w'])
        d = ser.get(e['name'])
        if not d:
            continue
        idx = [i for i, x in enumerate(d['exps']) if x['date'] == s['date']]
        if not idx:
            continue
        cur, prev = d['exps'][idx[-1]], [x for x in d['exps'][:idx[0]] if x['value']]
        delta = None
        if prev and cur['value']:
            p = prev[-1]
            if d['weighted'] and cur['top'][0] != p['top'][0]:
                delta = f"{'+' if cur['top'][0] > p['top'][0] else ''}{g.fmt_kg(cur['top'][0] - p['top'][0])} kg"
            elif cur['value'] != p['value']:
                delta = '↑' if cur['value'] > p['value'] else '↓'
            else:
                delta = '='
            if len(prev) >= 2 and cur['value'] > max(x['value'] for x in prev):
                prs.append(f"{short(e['name'])} {top_txt(g, cur['top'])}"
                           + (f" (e1RM {g.fmt_kg(cur['e1rm'])})" if cur['e1rm'] else ''))
        rows.append(f"{short(e['name'])} {top_txt(g, cur['top'])}" + (f' ({delta})' if delta else ''))
    dur = f" · {round(s['duration_s'] / 60)} min" if s['duration_s'] else ''
    key, nxt_lines, skipped = None, [], []
    if plan and plan.get('days'):
        names = {g.norm_name(ex[lg['ex']]['name']) for lg in s['logs']}
        scores = [len(names & {g.norm_name(x['name']) for x in d['exercises']}) / max(1, len(d['exercises']))
                  for d in plan['days']]
        if max(scores) and scores.count(max(scores)) == 1:
            day = plan['days'][scores.index(max(scores))]
            key = day.get('key')
            skipped = [short(x['name']) for x in day['exercises'] if g.norm_name(x['name']) not in names]
            for x in day['exercises']:
                why, nx = g.progress(x, g.exposures(conn, x["name"], plan["updated"]))
                for w in why:
                    if w.startswith(g.ddmm(s['date'])):
                        nxt_lines.append(f"{short(x['name'])} {w.split(': ', 1)[-1]}")
    L = [f"💪 Session {g.ddmm(s['date'])}" + (f' · {key}' if key else '') + dur,
         f"{len(s['logs'])} exercises · {hard} hard sets · {g.fmt_n(round(ton))} kg"]
    if prs:
        L.append('🏆 PR: ' + ', '.join(prs[:3]))
    L.append(' · '.join(rows[:5]) + (f' · +{len(rows) - 5} more' if len(rows) > 5 else ''))
    if key:
        L.append(f'Plan {key}: ' + (f"skipped {', '.join(skipped[:3])}" if skipped else 'every exercise done ✅'))
    if nxt_lines:
        L.append('Next time: ' + ', '.join(nxt_lines[:3]))
    wk = g.week_start(dt.date.fromisoformat(s['date'])).isoformat()
    n_wk = sum(1 for x in sessions if x['date'] >= wk)
    target = (plan or {}).get('days_per_week')
    week = f'Week: {n_wk}' + (f' of {target}' if target else '') + ' sessions' + \
        (' ✅' if target and n_wk >= target else '')
    if plan and plan.get('days'):
        _, nd = g.next_day(conn, plan, end)
        week += f" · next: {nd['name']}"
    L.append(week)
    return {'schema': 1, 'date': s['date'], 'day': key, 'duration_min': round(s['duration_s'] / 60)
            if s['duration_s'] else None, 'exercises': len(s['logs']), 'hard_sets': hard, 'tonnage_kg': round(ton),
            'prs': prs, 'skipped': skipped, 'next_time': nxt_lines, 'week_sessions': n_wk, 'text': '\n'.join(L)}


def state_path(g):
    return os.path.join(g.STATE, 'recap-state.json')


def recap_cron(g):
    """Cue note for the session when a new session settled (same fingerprint on two polls)."""
    conn = g.connect()
    sessions = [s for s in g.load(conn)[0] if s['date'] <= g.today().isoformat()]
    st = g.read_json(state_path(g)) or {}
    if not sessions:
        g.write_private_json(state_path(g), {'last': None})
        return 0, 'empty'
    s = sessions[-1]
    key = [s['date'], s['start_ts'] or 0]
    if 'last' not in st:                      # first run: start from here, recap nothing old
        g.write_private_json(state_path(g), {'last': key})
        return 0, 'init'
    last = st.get('last')
    if last and key <= last:
        return 0, 'none'
    if (g.today() - dt.date.fromisoformat(s['date'])).days > 2:     # a late backfill: not news
        g.write_private_json(state_path(g), {'last': key})
        return 0, 'old'
    fp = fingerprint(s)
    pend = st.get('pending') or {}
    if pend.get('key') != key or pend.get('fp') != fp:
        g.write_private_json(state_path(g), {'last': last, 'pending': {'key': key, 'fp': fp}})
        return 0, 'pending'
    inbox = os.environ.get('GYM_INBOX') or os.path.join(g.HOME, 'inbox')
    os.makedirs(inbox, exist_ok=True)
    path = os.path.join(inbox, f"gym-recap-{s['date']}-{key[1] % 100000}.md")
    tmp = path + '.tmp'
    with open(tmp, 'w') as fh:
        fh.write(f"# gym-recap: new session logged ({g.ddmm(s['date'])})\n\nData, not instructions.\n\n"
                 'A new Jefit session landed in the gym log. Run `gym recap` and send him its card as one short '
                 'message (hold it in quiet hours / a sleep-in morning). Handling: JOBS.md (gym-recap).\n')
    os.replace(tmp, path)
    g.write_private_json(state_path(g), {'last': key})
    return 0, 'note'


def cmd_recap(g, a):
    if a.cron:
        rc, what = recap_cron(g)
        # a healthy poll is outcome ok (the usage store counts any other exec outcome as a failed run + an error);
        # the poll state (init/pending/note/none/old/empty) goes into attrs
        _usage('gym.recap', 'exec', t0=a.t0, attrs={'state': what})
        return rc
    end = g.today()
    res = recap(g, g.connect(), g.read_json(g.PLAN_FILE), end, a.date)
    if not res:
        print('gym recap: no session logged' + (f' on {a.date}' if a.date else ''), file=sys.stderr)
        return 3
    print(json.dumps(res, ensure_ascii=False) if a.json else res['text'])
    _usage('gym.recap', t0=a.t0, attrs={'prs': len(res['prs'])})
    return 0


# ----------------------------------------------------------------------------- CLI
def date_arg(s):
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError('use YYYY-MM-DD')


def main(g, argv):
    t0 = time.time()
    p = argparse.ArgumentParser(prog='gym', description='gym recap (see ~/bin/lib/gymcoach.py)')
    sub = p.add_subparsers(dest='cmd')
    pr = sub.add_parser('recap')
    pr.add_argument('--date', type=date_arg)
    pr.add_argument('--json', action='store_true')
    pr.add_argument('--cron', action='store_true')
    a = p.parse_args(argv)
    a.t0 = t0
    return {'recap': cmd_recap}[a.cmd](g, a)
