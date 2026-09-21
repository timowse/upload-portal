#!/usr/bin/env python3
"""Checks for the drop.

    docker compose exec upload-portal python test_portal.py
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
import time
from urllib.parse import quote

DATA = tempfile.mkdtemp(prefix='portal-test-')
os.environ['PORTAL_DATA'] = DATA
os.environ['PORTAL_ORIGIN'] = 'https://upload.t1mo.dev'
os.environ['PORTAL_PUBLIC_BASE'] = 'https://up.t1mo.dev'
os.environ['PORTAL_MAX_GB'] = '0.01'          # 10 MB, so the ceiling is testable
os.environ['PORTAL_UPLOADS_PER_HOUR'] = '500'

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402
import media  # noqa: E402

importlib.reload(db)
importlib.reload(media)
import app as appmod  # noqa: E402

importlib.reload(appmod)
from fastapi.testclient import TestClient  # noqa: E402

passed = failed = 0


def check(label: str, cond: bool, extra: object = '') -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f'  PASS  {label}')
    else:
        failed += 1
        print(f'  FAIL  {label} {extra}')


def section(title: str) -> None:
    print(f'\n{title}')


def upload(c, name: str, payload: bytes, piece: int = 300_000):
    init = c.post('/api/upload/init', json={'name': name, 'size': len(payload)})
    if init.status_code != 200:
        return init
    uid = init.json()['uploadId']
    for i, off in enumerate(range(0, len(payload), piece)):
        c.put('/api/upload/part', params={'id': uid, 'i': i}, content=payload[off:off + piece])
    return c.post('/api/upload/done', params={'id': uid})


with TestClient(appmod.app) as c:
    section('Jeder darf hochladen, ohne Anmeldung')
    check('Einstellungen abrufbar', c.get('/api/config').json()['days'] == db.DEFAULT_DAYS)
    check('Upload-Seite wird ausgeliefert', '<title>Datei teilen</title>' in c.get('/').text)
    payload = bytes(range(256)) * 4000
    done = upload(c, '../../böse Datei.HEIC', payload)
    check('Upload nimmt an', done.status_code == 200, done.text)
    result = done.json()
    check('Dateiname entschaerft', '/' not in result['name'] and '..' not in result['name'],
          result['name'])
    check('Link kommt sofort zurueck', result['link'].startswith('https://up.t1mo.dev/s/'), result)
    check('Ablauf wird genannt', result['days'] == db.DEFAULT_DAYS, result)
    token = result['link'].rsplit('/', 1)[-1]

    section('Die Bytes stimmen')
    got = c.get(f'/s/{token}/file')
    check('Datei byte-genau zurueck', got.content == payload)
    check('Groesse stimmt', len(got.content) == len(payload))

    section('Grenzen')
    check('zu grosse Ankuendigung -> 413',
          c.post('/api/upload/init', json={'name': 'gross.bin', 'size': 50 * 1024 * 1024}
                 ).status_code == 413)
    lie = c.post('/api/upload/init', json={'name': 'lug.bin', 'size': 10}).json()
    check('mehr Daten als angekuendigt -> 413',
          c.put('/api/upload/part', params={'id': lie['uploadId'], 'i': 0},
                content=b'x' * 500).status_code == 413)
    check('leere Datei abgelehnt',
          c.post('/api/upload/init', json={'name': 'leer', 'size': 0}).status_code == 400)
    check('Pfad-Traversal in der Upload-ID -> 400',
          c.post('/api/upload/done', params={'id': '../../etc'}).status_code == 400)
    ooo = c.post('/api/upload/init', json={'name': 'a.bin', 'size': 100}).json()
    check('Teil ausser der Reihe -> 409',
          c.put('/api/upload/part', params={'id': ooo['uploadId'], 'i': 5},
                content=b'x').status_code == 409)

    section('Der Link')
    meta = c.get(f'/s/{token}/meta').json()
    check('Metadaten stimmen', meta['size'] == len(payload) and meta['kind'] == 'image', meta)
    check('Resttage werden gezaehlt', 0 < meta['daysLeft'] <= db.DEFAULT_DAYS, meta)
    page = c.get(f'/s/{token}')
    check('Vorschauseite liefert HTML', page.status_code == 200 and '<title>' in page.text)
    check('Open-Graph-Titel gesetzt', f'og:title" content="{meta["name"]}"' in page.text)
    disp = c.get(f'/s/{token}/dl').headers.get('content-disposition', '')
    # RFC 6266: an ASCII fallback plus the real name percent-encoded.
    check('Download traegt den Namen',
          f"filename*=UTF-8''{quote(meta['name'])}" in disp and disp.startswith('attachment'),
          disp)
    check('unbekannter Link -> 404', c.get('/s/gibtsnicht123').status_code == 404)

    section('Byte-Bereiche, ohne die kein Video laeuft')
    check('Accept-Ranges gesetzt', got.headers.get('accept-ranges') == 'bytes')
    part = c.get(f'/s/{token}/file', headers={'Range': 'bytes=0-99'})
    check('206 mit Content-Range',
          part.status_code == 206 and
          part.headers.get('content-range') == f'bytes 0-99/{len(payload)}', part.headers)
    check('Bereich byte-genau', part.content == payload[:100])
    suffix = c.get(f'/s/{token}/file', headers={'Range': 'bytes=-64'})
    check('Suffix-Bereich', suffix.status_code == 206 and suffix.content == payload[-64:])
    check('hinter dem Ende -> 416',
          c.get(f'/s/{token}/file', headers={'Range': 'bytes=99999999-'}).status_code == 416)

    section('Ablauf')
    entry = db.list_files()[0]
    with db.locked():
        data = db.load()
        data['files'][entry['id']]['expires'] = int(time.time()) - 10
        db.save(data)
    check('abgelaufener Link antwortet nicht mehr', c.get(f'/s/{token}').status_code == 404)
    gone = db.sweep()
    check('Aufraeumen entfernt ihn', entry['id'] in gone, gone)
    check('Datei ist von der Platte weg', not db.blob(entry['id']).exists())
    check('Index ist leer', db.stats()['files'] == 0)

    section('Tempodrossel')
    appmod.UPLOADS_PER_HOUR = 2
    appmod._recent.clear()
    codes = [c.post('/api/upload/init', json={'name': 'x.bin', 'size': 10}).status_code
             for _ in range(4)]
    check('bremst nach dem Limit', codes[:2] == [200, 200] and codes[-1] == 429, codes)

    section('CORS')
    allow = c.options('/api/config', headers={'Origin': 'https://upload.t1mo.dev',
                                              'Access-Control-Request-Method': 'GET'}).headers
    check('Pages-Origin erlaubt', allow.get('access-control-allow-origin') == 'https://upload.t1mo.dev')
    deny = c.options('/api/config', headers={'Origin': 'https://evil.example',
                                             'Access-Control-Request-Method': 'GET'}).headers
    check('fremdes Origin abgelehnt', 'access-control-allow-origin' not in deny)

shutil.rmtree(DATA, ignore_errors=True)
print(f'\n=== {passed} bestanden, {failed} fehlgeschlagen ===')
sys.exit(1 if failed else 0)
