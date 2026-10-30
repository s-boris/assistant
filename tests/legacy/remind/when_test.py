#!/usr/bin/env python3
"""Tests for ~/bin/lib/remindwhen.py (natural reminder times). REMINDWHEN_LIB = dir of a copy under test.
Fixed clock Wed 2026-10-21 10:00; synthetic calendar events. Exit 0 = all pass."""
import os, sys
from datetime import datetime
sys.path.insert(0, os.environ.get('REMINDWHEN_LIB') or '/home/node/bin/lib')
import remindwhen as W

NOW = datetime(2026, 10, 21, 10, 0)
EV = [
    {'start': '2026-10-21T21:00:00+02:00', 'end': '2026-10-21T22:00:00+02:00', 'all_day': False, 'title': 'Dance class',
     'free': False, 'flags': {'travel': None}},
    {'start': '2026-10-22T18:14:00+02:00', 'end': '2026-10-22T19:23:00+02:00', 'all_day': False,
     'title': '🚆 Home → Town', 'free': False, 'flags': {'travel': 'train'}},
    {'start': '2026-10-22T19:00:00+02:00', 'end': '2026-10-22T22:30:00+02:00', 'all_day': False, 'title': 'Big Band Live',
     'free': True, 'flags': {'travel': None}},
    {'start': '2026-10-24T16:00:00+02:00', 'end': '2026-10-25T02:00:00+02:00', 'all_day': False, 'title': 'Wedding Anna',
     'free': True, 'flags': {'travel': None}},
    {'start': '2026-10-27', 'end': '2026-10-28', 'all_day': True, 'title': '@Office', 'free': True, 'flags': {}},
    {'start': '2026-11-06T20:15:00+01:00', 'end': '2026-11-06T21:45:00+01:00', 'all_day': False,
     'title': '✈️ XY 153 AAA → BBB', 'free': False, 'flags': {'travel': 'flight'}},
]
cases = [
    ('2026-10-23 18:00', 'at', '2026-10-23 18:00'),
    ('2026-10-23', 'at', '2026-10-23 09:00'),
    ('+1h', 'at', '2026-10-21 11:00'),
    ('in 2 hours', 'at', '2026-10-21 12:00'),
    ('in 20 min', 'at', '2026-10-21 10:20'),
    ('in half an hour', 'at', '2026-10-21 10:30'),
    ('in 2 Stunden', 'at', '2026-10-21 12:00'),
    ('in einer Stunde', 'at', '2026-10-21 11:00'),
    ('in 3 days', 'at', '2026-10-24 10:00'),
    ('tomorrow', 'at', '2026-10-22 09:00'),
    ('tomorrow evening', 'at', '2026-10-22 19:00'),
    ('morgen früh', 'at', '2026-10-22 09:00'),
    ('morgen abend', 'at', '2026-10-22 19:00'),
    ('übermorgen um 14 Uhr', 'at', '2026-10-23 14:00'),
    ('tonight', 'at', '2026-10-21 19:00'),
    ('heute abend', 'at', '2026-10-21 19:00'),
    ('this afternoon', 'at', '2026-10-21 15:00'),
    ('friday 18:00', 'at', '2026-10-23 18:00'),
    ('Freitag um 18 Uhr', 'at', '2026-10-23 18:00'),
    ('next monday at 7', 'at', '2026-10-26 07:00'),
    ('wednesday', 'at', '2026-10-28 09:00'),
    ('friday evening at 8', 'at', '2026-10-23 20:00'),
    ('at 7', 'at', '2026-10-21 19:00'),
    ('6pm', 'at', '2026-10-21 18:00'),
    ('7 am', 'at', '2026-10-22 07:00'),
    ('18h', 'at', '2026-10-21 18:00'),
    ('9:30', 'at', '2026-10-22 09:30'),
    ('23.10.', 'at', '2026-10-23 09:00'),
    ('23.10.2026 18:30', 'at', '2026-10-23 18:30'),
    ('this weekend', 'at', '2026-10-24 10:00'),
    ('next week', 'at', '2026-10-26 09:00'),
    ('after lunch', 'at', '2026-10-21 13:30'),
    ('before bed', 'at', '2026-10-21 22:00'),
    ('after the concert', 'at', '2026-10-22 22:30'),
    ('nach dem Konzert', 'at', '2026-10-22 22:30'),
    ('before the wedding', 'at', '2026-10-24 15:00'),
    ('2h before the wedding', 'at', '2026-10-24 14:00'),
    ('before my flight', 'at', '2026-11-06 17:15'),
    ('vor dem Flug', 'at', '2026-11-06 17:15'),
    ('before the train', 'at', '2026-10-22 17:59'),
    ('after dance class', 'at', '2026-10-21 22:00'),
    ('30 min before dance class', 'at', '2026-10-21 20:30'),
    ('at the wedding', 'at', '2026-10-24 16:00'),
    ('nach der Hochzeit', 'at', '2026-10-25 02:00'),
    ('when I get home', 'on', 'home'),
    ("when i'm home", 'on', 'home'),
    ('wenn ich heimkomme', 'on', 'home'),
    ('wenn ich nach Hause komme', 'on', 'home'),
    ('when I leave', 'on', 'leave'),
    ('when I leave the house', 'on', 'leave'),
    ('when I leave work', 'on', 'leave:zone:Work'),
    ('after work', 'on', 'leave:zone:Work'),
    ('nach der Arbeit', 'on', 'leave:zone:Work'),
    ('when I get to work', 'on', 'zone:Work'),
    ('at work', 'on', 'zone:Work'),
    ('wenn ich im Büro bin', 'on', 'zone:Work'),
    ('when I arrive at the gym', 'on', 'zone:Gym'),
]
errors = ['', 'blah blah', 'after the dentist', 'in 0 min', '2026-10-21 09:00 x', 'yesterday']
pas = fail = 0
for phrase, kind, want in cases:
    try:
        w = W.parse(phrase, NOW, EV)
        got = (w.kind, w.at.strftime(W.FMT) if w.at else w.on)
    except ValueError as e:
        got = ('error', str(e))
    if got == (kind, want):
        pas += 1
    else:
        fail += 1
        print(f'FAIL - {phrase!r}: got {got}, want {(kind, want)}')
for phrase in errors:
    try:
        w = W.parse(phrase, NOW, EV)
        fail += 1
        print(f'FAIL - {phrase!r} should be an error, got {w.as_dict()}')
    except ValueError:
        pas += 1
try:
    W.parse('after the concert', NOW, None)
    fail += 1; print('FAIL - no calcache should be an error')
except ValueError as e:
    pas += 1 if 'calcache' in str(e) else 0
w = W.parse('after the concert', NOW, EV)
if w.anchor and w.anchor['event'] == 'Big Band Live' and 'after' in w.why:
    pas += 1
else:
    fail += 1; print('FAIL - anchor detail', w.as_dict())
print(f'when: {pas} passed, {fail} failed')
sys.exit(1 if fail else 0)
