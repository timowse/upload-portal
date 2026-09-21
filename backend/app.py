"""A public file drop.

Anyone may upload; the answer is a link to hand over. Files delete
themselves after a while, which is what keeps an open service from
growing without bound.

Nothing here authenticates a caller, so the ceilings matter: a size
limit, a floor of free disk that uploads may not eat into, and a cap on
how many uploads one address may start per hour.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

import db
import media

ALLOWED_ORIGIN = os.environ.get('PORTAL_ORIGIN', 'https://share.t1mo.dev')
PUBLIC_BASE = os.environ.get('PORTAL_PUBLIC_BASE', 'https://share.t1mo.dev').rstrip('/')
UPLOADS_PER_HOUR = int(os.environ.get('PORTAL_UPLOADS_PER_HOUR', '30'))
READ_CHUNK = 256 * 1024

HERE = Path(__file__).parent


def _page(name: str) -> Path:
    """In the image everything sits together; in a checkout index.html is
    one level up, where GitHub Pages serves it from."""
    here = HERE / name
    return here if here.exists() else HERE.parent / name


VIEWER = _page('viewer.html')
UPLOAD_PAGE = _page('index.html')

app = FastAPI(title='Upload Portal', docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOWED_ORIGIN.split(',') if o.strip()],
    allow_methods=['GET', 'POST', 'PUT', 'OPTIONS'],
    allow_headers=['Content-Type'],
    max_age=600,
)

_recent: dict[str, deque] = defaultdict(deque)


def _caller(request: Request) -> str:
    """Cloudflare puts the real address here; the socket shows the tunnel."""
    return (request.headers.get('cf-connecting-ip')
            or request.headers.get('x-forwarded-for', '').split(',')[0].strip()
            or (request.client.host if request.client else 'unbekannt'))


def _rate_limit(request: Request) -> None:
    now = time.time()
    seen = _recent[_caller(request)]
    while seen and now - seen[0] > 3600:
        seen.popleft()
    if len(seen) >= UPLOADS_PER_HOUR:
        raise HTTPException(status_code=429,
                            detail='Zu viele Uploads in kurzer Zeit. Später nochmal.')
    seen.append(now)
    if len(_recent) > 5000:                      # keep the bookkeeping bounded
        for key in [k for k, v in _recent.items() if not v][:1000]:
            _recent.pop(key, None)


@app.on_event('startup')
async def _startup() -> None:
    db.ensure_dirs()
    moved = db.migrate()
    if moved:
        print(f'{moved} Dateien aus dem alten Layout übernommen')

    async def sweeper() -> None:
        while True:
            try:
                gone = await asyncio.to_thread(db.sweep)
                if gone:
                    print(f'{len(gone)} abgelaufene Dateien entfernt')
            except Exception as err:                       # never kill the loop
                print(f'Aufräumen fehlgeschlagen: {err}')
            await asyncio.sleep(3600)

    asyncio.create_task(sweeper())


@app.get('/healthz')
def healthz() -> dict:
    return {'ok': True}


@app.get('/', response_class=HTMLResponse)
def home() -> HTMLResponse:
    """The same page GitHub Pages serves, so both hostnames work alone."""
    return HTMLResponse(UPLOAD_PAGE.read_text(encoding='utf-8'),
                        headers={'Cache-Control': 'no-cache'})


@app.get('/api/config')
def config() -> dict:
    return {
        'chunkSize': db.CHUNK_SIZE,
        'baseBytes': db.BASE_BYTES,
        'maxBytes': db.MAX_BYTES,
        'stepBytes': db.STEP_BYTES,
        'days': db.DEFAULT_DAYS,
        'minDays': db.MIN_DAYS,
        # What each step up costs in storage time, so the page can say it
        # before anyone commits to sending something.
        'scale': db.scale(),
        'accepting': db.accepting(0),
        'base': PUBLIC_BASE,
    }


# ----------------------------------------------------------------- upload

def _session_dir(upload_id: str) -> Path:
    if not db.SAFE_ID.match(upload_id or ''):
        raise HTTPException(status_code=400, detail='ungültige Upload-ID')
    return db.TMP_DIR / upload_id


@app.post('/api/upload/init')
async def upload_init(request: Request) -> dict:
    _rate_limit(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail='ungültiger Request')

    try:
        size = int(body.get('size'))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail='Größe fehlt')
    if size <= 0:
        raise HTTPException(status_code=400, detail='Die Datei ist leer')
    if size > db.MAX_BYTES:
        raise HTTPException(status_code=413,
                            detail=f'Maximal {db.MAX_BYTES // db.GB} GB pro Datei')
    if not db.accepting(size):
        raise HTTPException(status_code=507, detail='Kein Platz mehr frei')

    upload_id = db.token(12)
    session = _session_dir(upload_id)
    session.mkdir(parents=True, exist_ok=True)
    (session / 'meta.json').write_text(json.dumps({
        'name': db.safe_name(body.get('name')),
        'size': size,
        'received': 0,
        'nextIndex': 0,
    }), encoding='utf-8')
    return {'uploadId': upload_id, 'chunkSize': db.CHUNK_SIZE}


@app.put('/api/upload/part')
async def upload_part(request: Request, id: str = Query(...), i: int = Query(...)) -> dict:
    session = _session_dir(id)
    meta_path = session / 'meta.json'
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail='Upload nicht gefunden')

    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    if i != meta['nextIndex']:
        raise HTTPException(status_code=409, detail=f"Teil {meta['nextIndex']} erwartet")

    received = int(meta['received'])
    part = session / 'data.part'
    # Append while streaming, so a 32 MB chunk never has to fit in memory,
    # and stop the moment more arrives than was announced.
    with open(part, 'ab') as fh:
        async for chunk in request.stream():
            received += len(chunk)
            if received > int(meta['size']):
                fh.close()
                shutil.rmtree(session, ignore_errors=True)
                raise HTTPException(status_code=413, detail='mehr Daten als angekündigt')
            fh.write(chunk)

    meta['received'] = received
    meta['nextIndex'] = i + 1
    meta_path.write_text(json.dumps(meta), encoding='utf-8')
    return {'received': received, 'nextIndex': meta['nextIndex']}


@app.post('/api/upload/done')
def upload_done(id: str = Query(...)) -> dict:
    session = _session_dir(id)
    meta_path = session / 'meta.json'
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail='Upload nicht gefunden')

    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    part = session / 'data.part'
    if not part.exists() or part.stat().st_size != int(meta['size']):
        shutil.rmtree(session, ignore_errors=True)
        raise HTTPException(status_code=400, detail='Upload unvollständig')

    entry = db.add_file(meta['name'], meta['size'])
    os.replace(part, db.blob(entry['id']))
    shutil.rmtree(session, ignore_errors=True)
    media.thumbnail(entry['id'], entry['name'])          # ready for the first visitor
    return {
        'ok': True,
        'name': entry['name'],
        'link': f"{PUBLIC_BASE}/s/{entry['share']}",
        'expires': entry['expires'],
        'days': round((entry['expires'] - entry['uploaded']) / 86400),
    }


# ------------------------------------------------------------------ share

def _disposition(name: str, download: bool) -> str:
    ascii_name = re.sub(r'[^\x20-\x7e]', '_', name).replace('"', '')
    return (f'{"attachment" if download else "inline"}; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(name)}")


def _serve(path: Path, request: Request, mime: str, name: str, *,
           download: bool = False, max_age: int = 3600) -> Response:
    """Serve a file with byte ranges.

    Safari will not start a video at all unless a range request is answered
    with 206, and seeking depends on it everywhere, so this is required
    rather than an optimisation.
    """
    stat = path.stat()
    size = stat.st_size
    headers = {
        'Accept-Ranges': 'bytes',
        'ETag': f'"{int(stat.st_mtime)}-{size}"',
        'Cache-Control': f'private, max-age={max_age}',
        'Content-Disposition': _disposition(name, download),
    }

    start, end, status = 0, size - 1, 200
    rng = request.headers.get('range')
    if rng:
        m = re.match(r'bytes=(\d*)-(\d*)\s*$', rng.strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start >= size:
                return Response(status_code=416, headers={'Content-Range': f'bytes */{size}'})
            end = min(end, size - 1)
            status = 206
            headers['Content-Range'] = f'bytes {start}-{end}/{size}'

    length = end - start + 1
    headers['Content-Length'] = str(length)

    def body():
        with open(path, 'rb') as fh:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(READ_CHUNK, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    return StreamingResponse(body(), status_code=status, media_type=mime, headers=headers)


def _target(tok: str) -> dict:
    found = db.share_target(tok)
    if not found:
        raise HTTPException(status_code=404, detail='Link unbekannt oder abgelaufen')
    return found


def _meta(tok: str, f: dict) -> dict:
    kind = media.kind_of(f['name'])
    info = {
        'name': f['name'],
        'size': f['size'],
        'kind': kind,
        'mime': media.mime_of(f['name']),
        'playable': media.playable(f['name']) if kind == 'video' else True,
        'base': f'{PUBLIC_BASE}/s/{tok}',
        'expires': f['expires'],
        'daysLeft': max(0, round((f['expires'] - time.time()) / 86400)),
        'hasThumb': media.thumbnail(f['id'], f['name']) is not None,
    }
    info.update(media.probe(f['id'], f['name']))
    return info


@app.get('/s/{token}/meta')
def share_meta(token: str) -> dict:
    return _meta(token, _target(token))


@app.get('/s/{token}/file')
def share_file(request: Request, token: str) -> Response:
    f = _target(token)
    return _serve(f['path'], request, media.mime_of(f['name']), f['name'])


@app.get('/s/{token}/dl')
def share_download(request: Request, token: str) -> Response:
    f = _target(token)
    return _serve(f['path'], request, media.mime_of(f['name']), f['name'], download=True)


@app.get('/s/{token}/preview')
def share_preview(request: Request, token: str) -> Response:
    f = _target(token)
    rendition = media.preview(f['id'], f['name'])
    if not rendition:
        raise HTTPException(status_code=415, detail='Keine Vorschau möglich')
    mime = 'image/jpeg' if rendition != f['path'] else media.mime_of(f['name'])
    return _serve(rendition, request, mime, f['name'], max_age=86400)


@app.get('/s/{token}/thumb')
def share_thumb(request: Request, token: str) -> Response:
    f = _target(token)
    thumb = media.thumbnail(f['id'], f['name'])
    if not thumb:
        raise HTTPException(status_code=404, detail='Kein Vorschaubild')
    return _serve(thumb, request, 'image/jpeg', f['name'], max_age=86400)


def _escape(text: object) -> str:
    return (str(text).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


@app.get('/s/{token}', response_class=HTMLResponse)
def share_page(token: str) -> HTMLResponse:
    """The viewer.

    Rendered here rather than on Pages so a messenger fetching the link
    gets Open Graph tags and a thumbnail, and shows a preview card instead
    of a bare URL.
    """
    f = _target(token)
    info = _meta(token, f)
    db.count_view(token)

    html = VIEWER.read_text(encoding='utf-8')
    replacements = {
        '__TITLE__': info['name'],
        '__OG_TYPE__': {'image': 'article', 'video': 'video.other'}.get(info['kind'], 'website'),
        '__OG_IMAGE__': f"{info['base']}/thumb" if info['hasThumb'] else '',
        '__OG_URL__': info['base'],
        '__OG_DESC__': f"{info['size'] / 1024 / 1024:.1f} MB · noch {info['daysLeft']} Tage",
        '__META__': json.dumps(info),
    }
    for key, value in replacements.items():
        html = html.replace(key, value if key == '__META__' else _escape(value))
    return HTMLResponse(html, headers={'Cache-Control': 'no-store'})


# --------------------------------------------------------------------------
# Bundles: one link for several files, next to the link each one has anyway.
# --------------------------------------------------------------------------

BUNDLE_PAGE = _page('bundle.html')


@app.post('/api/bundle')
async def bundle_create(request: Request) -> dict:
    """Build a bundle from share links the caller already holds.

    Taking share tokens rather than file ids means this grants nothing new:
    whoever calls it could already reach every file they are naming.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail='ungültiger Request')

    tokens = body.get('tokens')
    if not isinstance(tokens, list) or not 2 <= len(tokens) <= 50:
        raise HTTPException(status_code=400, detail='Zwei bis fünfzig Dateien')

    ids = []
    for tok in tokens:
        found = db.share_target(str(tok))
        if not found:
            raise HTTPException(status_code=404, detail='Ein Link ist unbekannt oder abgelaufen')
        if found['id'] not in ids:
            ids.append(found['id'])

    token = db.create_bundle(ids)
    if not token:
        raise HTTPException(status_code=400, detail='Bündel konnte nicht angelegt werden')
    return {'link': f'{PUBLIC_BASE}/c/{token}', 'count': len(ids)}


def _bundle_or_404(token: str) -> dict:
    found = db.bundle(token)
    if not found:
        raise HTTPException(status_code=404, detail='Link unbekannt oder abgelaufen')
    return found


def _bundle_meta(found: dict) -> dict:
    files = []
    for f in found['files']:
        share = f.get('share')
        files.append({
            'name': f['name'],
            'size': f['size'],
            'kind': media.kind_of(f['name']),
            'link': f'{PUBLIC_BASE}/s/{share}',
            'thumb': f'{PUBLIC_BASE}/s/{share}/thumb',
            'hasThumb': media.thumbnail(f['id'], f['name']) is not None,
        })
    return {
        'count': len(files),
        'size': found['size'],
        'expires': found['expires'],
        'daysLeft': max(0, round((found['expires'] - time.time()) / 86400)),
        'base': f"{PUBLIC_BASE}/c/{found['token']}",
        'files': files,
    }


@app.get('/c/{token}/meta')
def bundle_meta(token: str) -> dict:
    return _bundle_meta(_bundle_or_404(token))


@app.get('/c/{token}', response_class=HTMLResponse)
def bundle_page(token: str) -> HTMLResponse:
    found = _bundle_or_404(token)
    info = _bundle_meta(found)
    cover = next((f for f in info['files'] if f['hasThumb']), None)

    html = BUNDLE_PAGE.read_text(encoding='utf-8')
    title = f"{info['count']} Dateien"
    replacements = {
        '__TITLE__': title,
        '__OG_IMAGE__': cover['thumb'] if cover else '',
        '__OG_URL__': info['base'],
        '__OG_DESC__': f"{info['size'] / 1024 / 1024:.1f} MB · noch {info['daysLeft']} Tage",
        '__META__': json.dumps(info),
    }
    for key, value in replacements.items():
        html = html.replace(key, value if key == '__META__' else _escape(value))
    return HTMLResponse(html, headers={'Cache-Control': 'no-store'})
