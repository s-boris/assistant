#!/usr/bin/env python3
"""Fake WLED JSON API (/w2, /w4, ...) + fake Deezer (/dz) for party tests. Fictional data only.
argv: DIR. Reads DIR/init.json (WLED states) and DIR/ctrl.json (down WLEDs, Deezer tracks) per request;
logs every write and every Deezer call to DIR/server.log; writes its port to DIR/port.
Test hooks: GET /_get/<w> (state), POST /_set/<w> (merge like a write, not logged), POST /_reset."""
import copy
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

D = sys.argv[1]
EFFECTS = ['Solid'] + [f'FX {i}' for i in range(1, 68)] + ['Bpm'] + [f'FX {i}' for i in range(69, 120)]
PALS = [f'Pal {i}' for i in range(40)]
for i, n in ((0, 'Default'), (6, 'Party'), (8, 'Lava'), (9, 'Ocean'), (11, 'Rainbow'), (13, 'Sunset'),
             (16, 'Red & Blue'), (35, 'Fire')):
    PALS[i] = n
STATES = {}


def reset():
    STATES.clear()
    STATES.update(json.load(open(os.path.join(D, 'init.json'))))


def ctrl():
    try:
        return json.load(open(os.path.join(D, 'ctrl.json')))
    except (OSError, ValueError):
        return {}


def logline(s):
    with open(os.path.join(D, 'server.log'), 'a') as fh:
        fh.write(s + '\n')


def merge(st, body):
    for k in ('on', 'bri', 'ps', 'pl'):
        if k in body:
            st[k] = body[k]
    for s in body.get('seg') or []:
        for seg in st['seg']:
            if seg['id'] == s.get('id'):
                seg.update({k: v for k, v in s.items() if k != 'id'})


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(n) or b'null') if n else None

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        parts = u.path.strip('/').split('/')
        if parts[0] == '_get':
            return self.send(STATES[parts[1]])
        if parts[0] == 'dz':
            if ctrl().get('dz_down'):
                return self.send({'error': 'down'}, 503)
            tracks = ctrl().get('tracks', [])
            if parts[1:] == ['search', 'track']:
                q = urllib.parse.parse_qs(u.query).get('q', [''])[0]
                logline('dz search ' + q)
                return self.send({'data': [{'id': t['id'], 'title': t['title'], 'title_short': t.get('title_short', t['title']),
                                            'artist': {'name': t['artist']}} for t in tracks
                                           if t['artist'].lower() in q.lower()], 'total': 1})
            if parts[1] == 'track':
                logline('dz track ' + parts[2])
                t = next((t for t in tracks if str(t['id']) == parts[2]), None)
                return self.send({'id': int(parts[2]), 'bpm': t['bpm']} if t else {'error': {}})
        w = parts[0]
        if w in ctrl().get('down', []) or w not in STATES:
            return self.send({'error': 'down'}, 500)
        if parts[1:] == ['json', 'state']:
            return self.send(STATES[w])
        if parts[1:] == ['json', 'effects']:
            return self.send(EFFECTS)
        if parts[1:] == ['json', 'palettes']:
            return self.send(PALS)
        self.send({'error': 'nf'}, 404)

    def do_POST(self):
        parts = self.path.strip('/').split('/')
        b = self.body()
        if parts[0] == '_reset':
            reset()
            return self.send({})
        if parts[0] == '_set':
            merge(STATES[parts[1]], b)
            return self.send(STATES[parts[1]])
        w = parts[0]
        if w in ctrl().get('down', []) or w not in STATES or parts[1:] != ['json', 'state']:
            return self.send({'error': 'down'}, 500)
        logline(f'{w} ' + json.dumps(b, sort_keys=True))
        if w in ctrl().get('ignore', []):  # accepts the write but does not apply it
            return self.send(STATES[w])
        merge(STATES[w], b)
        return self.send(copy.deepcopy(STATES[w]) if b.get('v') else {'success': True})


reset()
srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
with open(os.path.join(D, 'port'), 'w') as fh:
    fh.write(str(srv.server_address[1]))
srv.serve_forever()
