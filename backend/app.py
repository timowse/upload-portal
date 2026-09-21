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
