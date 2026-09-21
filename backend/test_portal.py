#!/usr/bin/env python3
"""Checks for the upload and share paths.

Run inside the container:
    docker compose exec upload-portal python test_portal.py
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile

DATA = tempfile.mkdtemp(prefix='portal-test-')
os.environ['PORTAL_DATA'] = DATA
os.environ['PORTAL_ORIGIN'] = 'https://upload.t1mo.dev'
os.environ['PORTAL_PUBLIC_BASE'] = 'https://up.t1mo.dev'
os.environ['PORTAL_ADMIN_PASSWORD'] = 'test-passwort-1234'

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auth  # noqa: E402
import shares  # noqa: E402
import store  # noqa: E402

importlib.reload(store)
importlib.reload(shares)
importlib.reload(auth)
import app as appmod  # noqa: E402

importlib.reload(appmod)
from fastapi.testclient import TestClient  # noqa: E402

c = TestClient(appmod.app)
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


section('Freigabe und Token')
check('geschlossen ohne Token', c.get('/api/status').json() == {'active': False})
check('kein Upload ohne Freigabe',
      c.post('/api/upload/init', json={'name': 'x', 'size': 5}).status_code == 403)
st = store.open_session(max_files=2, max_bytes=5 * 1024 * 1024, auto_close=True, note='Test')
tok = st['token']
check('falscher Token sieht nichts', c.get('/api/status', params={'t': 'nope'}).json() == {'active': False})
check('richtiger Token sieht die Freigabe',
      c.get('/api/status', params={'t': tok}).json().get('remaining') == 2)

section('Upload in Stuecken')
payload = bytes(range(256)) * 4000
init = c.post('/api/upload/init', params={'t': tok},
              json={'name': '../../böse Datei.HEIC', 'size': len(payload)}).json()
check('Dateiname entschaerft', '/' not in init['name'] and '..' not in init['name'], init['name'])
for i, off in enumerate(range(0, len(payload), 300_000)):
    r = c.put('/api/upload/part', params={'t': tok, 'id': init['uploadId'], 'i': i},
              content=payload[off:off + 300_000])
    check(f'Chunk {i}', r.status_code == 200, r.text)
check('Chunk ausser der Reihe abgelehnt',
      c.put('/api/upload/part', params={'t': tok, 'id': init['uploadId'], 'i': 99},
            content=b'x').status_code == 409)
done = c.post('/api/upload/done', params={'t': tok, 'id': init['uploadId']}).json()
check('Abschluss zaehlt runter', done['ok'] and done['remaining'] == 1, done)
landed = list(store.INCOMING_DIR.iterdir())
check('Datei byte-genau auf Platte', len(landed) == 1 and landed[0].read_bytes() == payload)

section('Grenzen werden serverseitig durchgesetzt')
check('zu grosse Ankuendigung -> 413',
      c.post('/api/upload/init', params={'t': tok},
             json={'name': 'gross.bin', 'size': 99 * 1024 * 1024}).status_code == 413)
lie = c.post('/api/upload/init', params={'t': tok}, json={'name': 'lug.bin', 'size': 10}).json()
check('mehr Daten als angekuendigt -> 413',
      c.put('/api/upload/part', params={'t': tok, 'id': lie['uploadId'], 'i': 0},
            content=b'x' * 500).status_code == 413)
check('Pfad-Traversal in der ID -> 400',
      c.post('/api/upload/done', params={'t': tok, 'id': '../../etc'}).status_code == 400)

section('Auto-Close')
second = c.post('/api/upload/init', params={'t': tok}, json={'name': 'zwei.txt', 'size': 4}).json()
c.put('/api/upload/part', params={'t': tok, 'id': second['uploadId'], 'i': 0}, content=b'abcd')
last = c.post('/api/upload/done', params={'t': tok, 'id': second['uploadId']}).json()
check('Session schliesst sich selbst', last['closed'] and last['remaining'] == 0, last)
check('Token danach wertlos', c.get('/api/status', params={'t': tok}).json() == {'active': False})

section('CORS')
allow = c.options('/api/status', headers={'Origin': 'https://upload.t1mo.dev',
                                          'Access-Control-Request-Method': 'GET'}).headers
check('Pages-Origin erlaubt', allow.get('access-control-allow-origin') == 'https://upload.t1mo.dev')
deny = c.options('/api/status', headers={'Origin': 'https://evil.example',
                                         'Access-Control-Request-Method': 'GET'}).headers
check('fremdes Origin abgelehnt', 'access-control-allow-origin' not in deny)

section('Teilen-Links')
name = landed[0].name
entry = shares.create(name)
share = entry['token']
check('unbekannter Token -> 404', c.get('/s/gibtsnicht123').status_code == 404)
meta = c.get(f'/s/{share}/meta').json()
check('Anzeigename ohne Zeitstempel', not meta['name'].startswith('2'), meta['name'])
check('gespeicherter Name bleibt erhalten', meta['stored'] == name)
page = c.get(f'/s/{share}')
check('Seite liefert HTML', page.status_code == 200 and '<title>' in page.text)
check('Open-Graph-Titel gesetzt', f'og:title" content="{meta["name"]}"' in page.text)
check('Download traegt den sauberen Namen',
      meta['name'] in c.get(f'/s/{share}/dl').headers.get('content-disposition', ''))

section('Byte-Bereiche, ohne die kein Video laeuft')
full = c.get(f'/s/{share}/file')
check('volle Datei mit Accept-Ranges', full.headers.get('accept-ranges') == 'bytes')
part = c.get(f'/s/{share}/file', headers={'Range': 'bytes=0-99'})
check('206 mit Content-Range', part.status_code == 206 and
      part.headers.get('content-range') == f'bytes 0-99/{len(payload)}', part.headers)
check('Bereich stimmt byte-genau', part.content == payload[:100])
suffix = c.get(f'/s/{share}/file', headers={'Range': 'bytes=-64'})
check('Suffix-Bereich', suffix.status_code == 206 and suffix.content == payload[-64:])
check('Bereich hinter dem Ende -> 416',
      c.get(f'/s/{share}/file', headers={'Range': 'bytes=99999999-'}).status_code == 416)

section('Zuruecknehmen')
check('Link zurueckgezogen', shares.revoke(share))
check('danach 404', c.get(f'/s/{share}').status_code == 404)

section('Oberflaeche: Anmeldung')
# Secure cookies are only kept over https, so this client speaks https.
a = TestClient(appmod.app, base_url='https://testserver')
check('ohne Anmeldung keine Dateiliste', a.get('/admin/api/files').status_code == 401)
check('Zustand sagt nicht angemeldet', a.get('/admin/api/state').json()['authed'] is False)
check('falsches Passwort abgelehnt',
      a.post('/admin/api/login', json={'password': 'daneben'}).status_code == 401)
check('richtiges Passwort angenommen',
      a.post('/admin/api/login', json={'password': 'test-passwort-1234'}).status_code == 200)
check('danach angemeldet', a.get('/admin/api/state').json()['authed'] is True)
check('Seite wird ausgeliefert', '<title>Dateien</title>' in a.get('/admin').text)

section('Oberflaeche: hochladen ohne Einmal-Token')
before = store.read()
check('Freigabe ist zu', not before['active'])
own = a.post('/api/upload/init', json={'name': 'eigenes.png', 'size': 9}).json()
check('Upload ohne Token erlaubt, weil angemeldet', 'uploadId' in own, own)
a.put('/api/upload/part', params={'id': own['uploadId'], 'i': 0}, content=b'123456789')
fin = a.post('/api/upload/done', params={'id': own['uploadId']}).json()
check('Upload abgeschlossen', fin['ok'], fin)
check('verbraucht keinen fremden Upload-Platz', store.read()['uploaded'] == before['uploaded'])

section('Oberflaeche: teilen und aufraeumen')
listing = a.get('/admin/api/files').json()['files']
mine = next(f for f in listing if f['name'] == 'eigenes.png')
link = a.post('/admin/api/share', json={'file': mine['stored']}).json()
check('Link erzeugt', link['link'].startswith('https://up.t1mo.dev/s/'), link)
again = a.post('/admin/api/share', json={'file': mine['stored']}).json()
check('zweimal teilen gibt denselben Link', again['token'] == link['token'])
check('Link funktioniert', c.get(f"/s/{link['token']}").status_code == 200)
check('Pfad-Traversal beim Teilen abgewehrt',
      a.post('/admin/api/share', json={'file': '../../etc/passwd'}).status_code in (400, 404))
check('Eingangslink laesst sich oeffnen',
      a.post('/admin/api/inbox', json={'files': 2, 'maxGb': 1}).json()['link'].startswith('https://upload'))
check('und wieder schliessen', a.post('/admin/api/inbox/close').json()['ok'])
check('Datei geloescht', a.post('/admin/api/delete', json={'file': mine['stored']}).json()['ok'])
check('Link danach tot', c.get(f"/s/{link['token']}").status_code == 404)
check('Abmelden funktioniert', a.post('/admin/api/logout').status_code == 200)
check('danach wieder gesperrt', a.get('/admin/api/files').status_code == 401)

shutil.rmtree(DATA, ignore_errors=True)
print(f'\n=== {passed} bestanden, {failed} fehlgeschlagen ===')
sys.exit(1 if failed else 0)
