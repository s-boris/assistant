"""SBB timetable deep links with from/to and departure time pre-filled.

    sbb_link('Zürich HB', 'Bern', '2026-01-31', '18:02')
    -> https://app.sbbmobile.ch/timetable?from=Z%C3%BCrich%20HB&to=Bern&time=1769878920

sbb_link (the default) opens the SBB Mobile app: exact SBB stop names percent-encoded (%20 for spaces) and the
departure as Unix epoch SECONDS, computed from the local Europe/Zurich date and time (DST-aware). Milliseconds,
ISO strings and a date= parameter do not work in the app.
The app's timetable link parser (SBB Mobile 12.55) also reads: from/to/via0..via2 = CURRENT_POSITION (the phone's own
location, filled by the SBB app), fromll/toll = "lat,lon", timemode = departure|arrival.

sbb_web_link builds the sbb.ch web timetable link instead (/en?stops=A~B&day=YYYY-MM-DD&time=HH_MM&moment=dep);
an optional numeric stop id (UIC, e.g. 8503000) pins a stop there as Name_I<id>. The app link takes no ids.

Use exact SBB stop names (a town name can resolve to an address).
CLI: python3 ~/bin/lib/sbb.py [--web] FROM TO YYYY-MM-DD HH:MM [FROM_ID TO_ID]
"""
import datetime as dt
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import assistant_profile  # noqa: E402

APP_BASE = 'https://app.sbbmobile.ch/timetable'
WEB_BASE = 'https://www.sbb.ch/en'
TZ = assistant_profile.tz()  # the owner's zone (profile locale.tz; SBB times are Swiss local time)


def _name(name):
    name = ' '.join(str(name or '').replace('~', ' ').split())
    if not name:
        raise ValueError('empty stop name')
    return name


def _stop(name, sid=None):
    sid = str(sid or '').strip()
    return urllib.parse.quote_plus(_name(name) + (f'_I{sid}' if re.fullmatch(r'\d{6,9}', sid) else ''))


def _day_time(day, time):
    """-> ('YYYY-MM-DD', 'HH:MM') in Europe/Zurich local time; aware datetimes are converted first."""
    if isinstance(time, dt.datetime) and time.tzinfo is not None:
        time = time.astimezone(TZ)
    if isinstance(day, dt.datetime) and day.tzinfo is not None:
        day = day.astimezone(TZ)
    if isinstance(day, (dt.date, dt.datetime)):
        day = day.strftime('%Y-%m-%d')
    if isinstance(time, (dt.time, dt.datetime)):
        time = time.strftime('%H:%M')
    day, time = str(day), str(time)
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) or not re.fullmatch(r'\d{1,2}:\d{2}', time):
        raise ValueError(f'bad date/time: {day} {time}')
    h, m = time.split(':')
    if int(h) > 23 or int(m) > 59:
        raise ValueError(f'bad time: {time}')
    return day, f'{int(h):02d}:{m}'


def epoch(day, time):
    """Unix epoch seconds of a Europe/Zurich local date + time (DST-aware)."""
    day, time = _day_time(day, time)
    try:
        local = dt.datetime.strptime(f'{day} {time}', '%Y-%m-%d %H:%M').replace(tzinfo=TZ)
    except ValueError as e:
        raise ValueError(f'bad date/time: {day} {time}') from e
    return int(local.timestamp())


def sbb_link(frm, to, day, time, frm_id=None, to_id=None):
    """SBB Mobile app link (default). day: date or 'YYYY-MM-DD'; time: time/datetime or 'HH:MM' (Zurich local).
    frm_id/to_id are accepted for compatibility and ignored (the app link takes stop names only)."""
    q = lambda s: urllib.parse.quote(_name(s), safe='')  # noqa: E731
    return f'{APP_BASE}?from={q(frm)}&to={q(to)}&time={epoch(day, time)}'


def sbb_web_link(frm, to, day, time, frm_id=None, to_id=None):
    """sbb.ch web timetable link (departure time)."""
    day, time = _day_time(day, time)
    h, m = time.split(':')
    return (f"{WEB_BASE}?stops={_stop(frm, frm_id)}~{_stop(to, to_id)}"
            f"&day={day}&time={h}_{m}&moment=dep")


if __name__ == '__main__':
    a = sys.argv[1:]
    fn = sbb_link
    if a[:1] == ['--web']:
        fn, a = sbb_web_link, a[1:]
    if len(a) not in (4, 6):
        sys.exit('usage: sbb.py [--web] FROM TO YYYY-MM-DD HH:MM [FROM_ID TO_ID]')
    try:
        print(fn(*a))
    except ValueError as e:
        sys.exit(f'sbb.py: {e}')
