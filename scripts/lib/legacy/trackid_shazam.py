#!/usr/bin/env python3
"""trackid_shazam.py WAV [WAV ...]: Shazam lookup for trackid (runs in ~/opt/trackid-venv).

For each WAV (a 16 kHz mono window cut by trackid) it computes the Shazam audio fingerprint
locally (shazamio_core, the Rust port of SongRec's algorithm) and POSTs ONLY that fingerprint
to Shazam's unofficial recognition endpoint. Audio never leaves the box.

Request body per window: {"timezone": TZ, "signature": {"uri": "data:audio/vnd.shazam.sig;base64,…",
"samplems": N}, "timestamp": ms, "context": {}, "geolocation": {}}; URL path carries two random UUIDs
(fresh per request, no account, no device id). Stdout: one JSON line per WAV:
{"file": WAV, "ok": true, "response": {...}} or {"file": WAV, "ok": false, "error": "..."}.
Env: TRACKID_SHAZAM_URL (test seam), TRACKID_TIMEOUT (s, default 15).
"""
import asyncio
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

from shazamio_core import Recognizer, SearchParams

URL = os.environ.get('TRACKID_SHAZAM_URL') or (
    'https://amp.shazam.com/discovery/v5/en-US/GB/android/-/tag/{u1}/{u2}'
    '?sync=true&webv3=true&sampling=true&connected=&shazamapiversion=v3&sharehub=true'
    '&hubv5minorversion=v5.1&hidelb=true&video=v3')
TIMEOUT = float(os.environ.get('TRACKID_TIMEOUT', '15'))
TZ = 'Europe/Berlin'        # generic CET zone, as the Shazam apps send one
UA = 'Dalvik/2.1.0 (Linux; U; Android 14; Pixel 8 Build/AP2A.240805.005)'
SEGMENT = 12                # seconds; Shazam accepts up to ~12 s per signature


def post(body):
    url = URL.format(u1=str(uuid.uuid4()).upper(), u2=str(uuid.uuid4()).upper())
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method='POST', headers={
        'Content-Type': 'application/json', 'Accept': 'application/json',
        'Accept-Encoding': 'gzip', 'Accept-Language': 'en-US', 'User-Agent': UA,
        'X-Shazam-Platform': 'ANDROID', 'X-Shazam-AppVersion': '14.1.0'})
    last = None
    for attempt in range(2):                     # one retry on 429/5xx/network, then give up
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
                if r.headers.get('Content-Encoding') == 'gzip':
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode() or '{}')
        except urllib.error.HTTPError as e:
            last = f'HTTP {e.code}'
            if e.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last = f'{type(e).__name__}: {e}'
        time.sleep(2 + 2 * attempt)
    raise RuntimeError(last or 'request failed')


async def main(paths):
    rec = Recognizer(segment_duration_seconds=SEGMENT)
    opts = SearchParams(segment_duration_seconds=SEGMENT)
    for p in paths:
        try:
            sig = await rec.recognize_path(value=p, options=opts)
            body = {'timezone': TZ,
                    'signature': {'uri': sig.signature.uri, 'samplems': sig.signature.samples},
                    'timestamp': int(time.time() * 1000), 'context': {}, 'geolocation': {}}
            out = {'file': p, 'ok': True, 'response': post(body)}
        except Exception as e:  # noqa: BLE001 - reported per window, never fatal
            out = {'file': p, 'ok': False, 'error': str(e)[:300]}
        print(json.dumps(out, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        print(__doc__.strip())
        sys.exit(0 if len(sys.argv) > 1 else 2)
    asyncio.run(main(sys.argv[1:]))
