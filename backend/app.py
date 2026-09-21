"""Upload endpoint for the portal.

The browser never posts a whole file in one request. It asks for an upload
slot, pushes the file in chunks that stay under Cloudflare's proxied body
limit, and then asks the server to finalise it. Every limit the portal
advertises is enforced here, because the page in front of it cannot
enforce anything a direct POST could not skip.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import store

ALLOWED_ORIGIN = os.environ.get('PORTAL_ORIGIN', 'https://upload.t1mo.dev')

app = FastAPI(title='Upload Portal', docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOWED_ORIGIN.split(',') if o.strip()],
    allow_methods=['GET', 'POST', 'PUT', 'OPTIONS'],
    allow_headers=['Content-Type'],
    max_age=600,
)


def _session_dir(upload_id: str) -> Path:
    if not store.SAFE_ID.match(upload_id):
        raise HTTPException(status_code=400, detail='ungueltige Upload-ID')
    return store.TMP_DIR / upload_id


def _require_open(token: str | None) -> dict:
    state = store.read()
    if not store.is_open(state, token):
        raise HTTPException(status_code=403, detail='keine aktive Freigabe')
    return state


@app.get('/healthz')
def healthz() -> dict:
    return {'ok': True}


@app.get('/api/status')
def status(t: str | None = Query(default=None)) -> JSONResponse:
    """Report the session behind this token.

    Without a valid token the answer is a flat "closed", so polling the
    endpoint reveals neither that a session exists nor its limits.
    """
    state = store.read()
    if not store.is_open(state, t):
        return JSONResponse({'active': False})
    return JSONResponse({
        'active': True,
        'maxFiles': state['maxFiles'],
        'remaining': store.remaining(state),
        'maxBytes': state['maxBytes'],
        'autoClose': state['autoClose'],
        'chunkSize': store.CHUNK_SIZE,
        'note': state['note'],
    })


@app.post('/api/upload/init')
async def upload_init(request: Request, t: str | None = Query(default=None)) -> dict:
    state = _require_open(t)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail='ungueltiger Request')

    name = store.safe_filename(body.get('name'))
    try:
        size = int(body.get('size'))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail='Groesse fehlt')
    if size <= 0:
        raise HTTPException(status_code=400, detail='leere Datei')
    if size > int(state['maxBytes']):
        raise HTTPException(status_code=413, detail='Datei ueberschreitet das Limit')

    upload_id = store.new_token()
    session = store.TMP_DIR / upload_id
    session.mkdir(parents=True, exist_ok=True)
    (session / 'meta.json').write_text(
        json.dumps({'name': name, 'size': size, 'received': 0, 'nextIndex': 0}),
        encoding='utf-8',
    )
    return {'uploadId': upload_id, 'chunkSize': store.CHUNK_SIZE, 'name': name}


@app.put('/api/upload/part')
async def upload_part(
    request: Request,
    t: str | None = Query(default=None),
    id: str = Query(...),
    i: int = Query(...),
) -> dict:
    state = _require_open(t)
    session = _session_dir(id)
    meta_path = session / 'meta.json'
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail='Upload nicht gefunden')

    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    if i != meta['nextIndex']:
        raise HTTPException(status_code=409, detail=f"Chunk {meta['nextIndex']} erwartet")

    received = int(meta['received'])
    limit = min(int(meta['size']), int(state['maxBytes']))
    part_path = session / 'data.part'

    # Append while streaming so a large chunk never has to fit in memory,
    # and stop the moment the client sends more than it declared.
    with open(part_path, 'ab') as fh:
        async for chunk in request.stream():
            received += len(chunk)
            if received > limit:
                fh.close()
                shutil.rmtree(session, ignore_errors=True)
                raise HTTPException(status_code=413, detail='mehr Daten als angekuendigt')
            fh.write(chunk)

    meta['received'] = received
    meta['nextIndex'] = i + 1
    meta_path.write_text(json.dumps(meta), encoding='utf-8')
    return {'received': received, 'nextIndex': meta['nextIndex']}


@app.post('/api/upload/done')
def upload_done(t: str | None = Query(default=None), id: str = Query(...)) -> dict:
    _require_open(t)
    session = _session_dir(id)
    meta_path = session / 'meta.json'
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail='Upload nicht gefunden')

    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    part_path = session / 'data.part'
    if not part_path.exists() or part_path.stat().st_size != int(meta['size']):
        shutil.rmtree(session, ignore_errors=True)
        raise HTTPException(status_code=400, detail='Upload unvollstaendig')

    target = store.unique_target(meta['name'])
    os.replace(part_path, target)
    shutil.rmtree(session, ignore_errors=True)

    state = store.count_upload()
    return {
        'ok': True,
        'name': target.name,
        'remaining': store.remaining(state),
        'closed': not state['active'],
    }


# --------------------------------------------------------------------------
# Sharing: a link that shows the file rather than just handing it over.
# --------------------------------------------------------------------------

import re  # noqa: E402
from urllib.parse import quote  # noqa: E402

from fastapi import Path as PathParam  # noqa: E402
from fastapi.responses import HTMLResponse, Response, StreamingResponse  # noqa: E402

import shares  # noqa: E402

PUBLIC_BASE = os.environ.get('PORTAL_PUBLIC_BASE', 'https://up.t1mo.dev').rstrip('/')
VIEWER = Path(__file__).with_name('viewer.html')
READ_CHUNK = 256 * 1024


def _disposition(name: str, download: bool) -> str:
    """Give plain clients an ASCII name and capable ones the real one."""
    ascii_name = re.sub(r'[^\x20-\x7e]', '_', name).replace('"', '')
    return (f'{"attachment" if download else "inline"}; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(name)}")


def _serve_file(path: Path, request: Request, mime: str, *, download: bool = False,
                max_age: int = 3600, name: str | None = None) -> Response:
    """Serve a file with byte ranges.

    Safari will not play a video at all unless the server answers a range
    request with 206, and seeking in any browser depends on it, so this is
    not an optimisation.
    """
    stat = path.stat()
    size = stat.st_size
    headers = {
        'Accept-Ranges': 'bytes',
        'ETag': f'"{int(stat.st_mtime)}-{size}"',
        'Cache-Control': f'private, max-age={max_age}',
        'Content-Disposition': _disposition(name or path.name, download),
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


def _share_or_404(token: str):
    found = shares.resolve(token)
    if not found:
        raise HTTPException(status_code=404, detail='Link unbekannt oder abgelaufen')
    return found


def _meta(token: str, entry: dict, path: Path) -> dict:
    kind = shares.kind_of(path.name)
    stat = path.stat()
    data = {
        'name': shares.display_name(path.name),
        'stored': path.name,
        'size': stat.st_size,
        'kind': kind,
        'mime': shares.mime_of(path.name),
        'playable': shares.playable(path.name) if kind == 'video' else True,
        'base': f'{PUBLIC_BASE}/s/{token}',
        'hasThumb': shares.thumbnail(token, path) is not None,
    }
    data.update(shares.probe(path))
    return data


@app.get('/s/{token}/meta')
def share_meta(request: Request, token: str = PathParam(...)) -> dict:
    entry, path = _share_or_404(token)
    return _meta(token, entry, path)


@app.get('/s/{token}/file')
def share_file(request: Request, token: str = PathParam(...)) -> Response:
    _entry, path = _share_or_404(token)
    return _serve_file(path, request, shares.mime_of(path.name))


@app.get('/s/{token}/dl')
def share_download(request: Request, token: str = PathParam(...)) -> Response:
    _entry, path = _share_or_404(token)
    return _serve_file(path, request, shares.mime_of(path.name), download=True,
                       name=shares.display_name(path.name))


@app.get('/s/{token}/preview')
def share_preview(request: Request, token: str = PathParam(...)) -> Response:
    """An image the browser can actually paint - HEIC becomes JPEG here."""
    _entry, path = _share_or_404(token)
    rendition = shares.preview_image(token, path)
    if not rendition:
        raise HTTPException(status_code=415, detail='Keine Vorschau moeglich')
    mime = 'image/jpeg' if rendition != path else shares.mime_of(path.name)
    return _serve_file(rendition, request, mime, max_age=86400)


@app.get('/s/{token}/thumb')
def share_thumb(request: Request, token: str = PathParam(...)) -> Response:
    _entry, path = _share_or_404(token)
    thumb = shares.thumbnail(token, path)
    if not thumb:
        raise HTTPException(status_code=404, detail='Kein Vorschaubild')
    return _serve_file(thumb, request, 'image/jpeg', max_age=86400)


@app.get('/s/{token}', response_class=HTMLResponse)
def share_page(request: Request, token: str = PathParam(...)) -> HTMLResponse:
    """The viewer itself.

    Rendered here rather than on Pages so that a messenger fetching the URL
    gets Open Graph tags and a thumbnail, and therefore shows a real preview
    card instead of a bare link.
    """
    entry, path = _share_or_404(token)
    meta = _meta(token, entry, path)
    shares.count_view(token)

    og_image = f"{meta['base']}/thumb" if meta['hasThumb'] else ''
    og_type = {'image': 'article', 'video': 'video.other'}.get(meta['kind'], 'website')
    html = VIEWER.read_text(encoding='utf-8')
    for key, value in {
        '__TITLE__': meta['name'],
        '__OG_TYPE__': og_type,
        '__OG_IMAGE__': og_image,
        '__OG_URL__': meta['base'],
        '__OG_DESC__': f"{meta['kind']} · {meta['size'] / 1024 / 1024:.1f} MB",
        '__META__': json.dumps(meta),
    }.items():
        html = html.replace(key, value if key == '__META__' else _escape(value))
    return HTMLResponse(html, headers={'Cache-Control': 'no-store'})


def _escape(text: str) -> str:
    return (str(text).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))
