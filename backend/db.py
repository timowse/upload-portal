"""Files and their links.

No accounts, no sessions: anyone may drop a file and gets a link back.
The index is one JSON file under an flock, replaced atomically, so a
crash mid-write cannot leave half an index behind.

Bytes live under blobs/<id> with an opaque name, and the name a person
sees is metadata. Two people uploading IMG_0001.HEIC therefore do not
collide, and nothing a caller types ever becomes a path.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.environ.get('PORTAL_DATA', '/data'))
DB_FILE = DATA_DIR / 'db.json'
LOCK_FILE = DATA_DIR / '.db.lock'
BLOB_DIR = DATA_DIR / 'blobs'
TMP_DIR = DATA_DIR / 'tmp'
THUMB_DIR = DATA_DIR / 'thumbs'

CHUNK_SIZE = 32 * 1024 * 1024                     # under Cloudflare's 100 MB body cap
GB = 1024 ** 3

# A file up to BASE_BYTES is kept DEFAULT_DAYS. Beyond that the size may be
# raised a gigabyte at a time up to MAX_BYTES, and the time it is kept
# shrinks to match.
BASE_BYTES = int(float(os.environ.get('PORTAL_BASE_GB', '5')) * GB)
MAX_BYTES = int(float(os.environ.get('PORTAL_MAX_GB', '10')) * GB)
STEP_BYTES = GB
DEFAULT_DAYS = int(os.environ.get('PORTAL_DAYS', '30'))
MIN_DAYS = int(os.environ.get('PORTAL_MIN_DAYS', '7'))
MAX_DAYS = 365
# Stop accepting uploads while less than this is free, so a full disk never
# takes the rest of the Pi down with it.
KEEP_FREE = int(float(os.environ.get('PORTAL_KEEP_FREE_GB', '5')) * 1024 ** 3)

SAFE_ID = re.compile(r'^[A-Za-z0-9_-]{6,64}$')
EMPTY: dict = {'files': {}, 'shares': {}, 'bundles': {}}


def ensure_dirs() -> None:
    for d in (DATA_DIR, BLOB_DIR, TMP_DIR, THUMB_DIR):
        d.mkdir(parents=True, exist_ok=True)


@contextmanager
def locked():
    ensure_dirs()
    with open(LOCK_FILE, 'w') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load() -> dict:
    ensure_dirs()
    try:
        data = json.loads(DB_FILE.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    return {'files': dict(data.get('files', {})),
            'shares': dict(data.get('shares', {})),
            'bundles': dict(data.get('bundles', {}))}


def save(data: dict) -> None:
    ensure_dirs()
    tmp = DB_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    os.replace(tmp, DB_FILE)


def token(length: int = 9) -> str:
    return secrets.token_urlsafe(length)


def blob(file_id: str) -> Path:
    return BLOB_DIR / file_id


def free_space() -> int:
    ensure_dirs()
    return shutil.disk_usage(DATA_DIR).free


def accepting(size: int) -> bool:
    return free_space() - size > KEEP_FREE


def days_for(size: int) -> int:
    """How long a file of this size is kept.

    A straight line from DEFAULT_DAYS at BASE_BYTES down to MIN_DAYS at
    MAX_BYTES. The exact line runs through numbers like 25.4 and 16.2, so
    the result is rounded to something worth reading: fives while the
    figure is large, whole days once it is small. Both ends land on their
    stated value exactly.
    """
    if size <= BASE_BYTES:
        return DEFAULT_DAYS
    ratio = min(1.0, (size - BASE_BYTES) / max(1, MAX_BYTES - BASE_BYTES))
    exact = DEFAULT_DAYS + (MIN_DAYS - DEFAULT_DAYS) * ratio
    days = round(exact / 5) * 5 if exact >= 10 else round(exact)
    return max(MIN_DAYS, min(DEFAULT_DAYS, int(days)))


def scale() -> list[dict]:
    """The steps the page offers, each with what it costs in days."""
    steps = []
    size = BASE_BYTES
    while size <= MAX_BYTES:
        steps.append({'bytes': size, 'days': days_for(size)})
        size += STEP_BYTES
    return steps


def safe_name(name: str) -> str:
    """Only ever used for display and the download header, never as a path."""
    name = os.path.basename(str(name or '')).replace('\x00', '')
    name = re.sub(r'[^\w.\-() \[\]]+', '_', name, flags=re.UNICODE).strip(' .')
    name = re.sub(r'_{2,}', '_', name)
    return (name or 'datei')[:120]


# ---------------------------------------------------------------- files

def add_file(name: str, size: int, days: int | None = None) -> dict:
    with locked():
        data = load()
        fid = token(12)
        # The size decides, not the caller: a small file keeps the full
        # period even when it arrived through a raised ceiling.
        keep = days_for(int(size)) if days is None else max(1, min(int(days), MAX_DAYS))
        share_token = token(9)
        data['files'][fid] = {
            'name': safe_name(name),
            'size': int(size),
            'uploaded': int(time.time()),
            'expires': int(time.time() + keep * 86400),
            'share': share_token,
        }
        # Every file is shareable the moment it lands: the upload is only
        # worth anything once there is a link to hand over.
        data['shares'][share_token] = {'file': fid, 'created': int(time.time()), 'views': 0}
        save(data)
        return {'id': fid, **data['files'][fid]}


def get_file(fid: str) -> dict | None:
    if not SAFE_ID.match(fid or ''):
        return None
    f = load()['files'].get(fid)
    return {'id': fid, **f} if f else None


def list_files() -> list[dict]:
    data = load()
    return sorted(({'id': fid, **f} for fid, f in data['files'].items()),
                  key=lambda f: f['uploaded'], reverse=True)


def delete_file(fid: str) -> bool:
    with locked():
        data = load()
        entry = data['files'].pop(fid, None)
        if not entry:
            return False
        data['shares'].pop(entry.get('share'), None)
        for bundle_entry in data['bundles'].values():
            bundle_entry['files'] = [f for f in bundle_entry['files'] if f != fid]
        save(data)
    blob(fid).unlink(missing_ok=True)
    for leftover in THUMB_DIR.glob(f'{fid}*'):
        leftover.unlink(missing_ok=True)
    return True


# --------------------------------------------------------------- shares

def share_target(tok: str) -> dict | None:
    """Resolve a link to a live, unexpired file."""
    if not SAFE_ID.match(tok or ''):
        return None
    data = load()
    entry = data['shares'].get(tok)
    if not entry:
        return None
    f = data['files'].get(entry['file'])
    if not f or time.time() > f['expires']:
        return None
    path = blob(entry['file'])
    if not path.is_file():
        return None
    return {'id': entry['file'], **f, 'path': path, 'views': entry.get('views', 0)}


def count_view(tok: str) -> None:
    with locked():
        data = load()
        if tok in data['shares']:
            data['shares'][tok]['views'] = int(data['shares'][tok].get('views', 0)) + 1
            save(data)


# -------------------------------------------------------------- bundles

def create_space() -> str:
    """An upload goes into a space, and the space is what gets shared.

    It exists before the first byte arrives, so the link can be shown
    straight away and every file of the batch lands in the same place -
    including the ones that only succeed on a second try.
    """
    with locked():
        data = load()
        tok = token(9)
        data['bundles'][tok] = {'files': [], 'created': int(time.time())}
        save(data)
        return tok


def space_add(tok: str, file_id: str) -> bool:
    if not SAFE_ID.match(tok or ''):
        return False
    with locked():
        data = load()
        entry = data['bundles'].get(tok)
        if entry is None or file_id not in data['files']:
            return False
        if file_id not in entry['files']:
            entry['files'].append(file_id)
            save(data)
        return True


def bundle(tok: str) -> dict | None:
    """Resolve a space, skipping members that expired or went missing.

    An empty space still resolves: it may simply be waiting for the first
    upload of a batch that is still running.
    """
    if not SAFE_ID.match(tok or ''):
        return None
    data = load()
    entry = data['bundles'].get(tok)
    if entry is None:
        return None
    now = time.time()
    items = []
    for fid in entry['files']:
        f = data['files'].get(fid)
        if not f or now > f['expires'] or not blob(fid).is_file():
            continue
        items.append({'id': fid, **f})
    # A space is only good while its shortest-lived member is.
    expires = min((f['expires'] for f in items),
                  default=int(entry['created'] + DEFAULT_DAYS * 86400))
    return {'token': tok, 'files': items, 'expires': expires,
            'size': sum(f['size'] for f in items)}


# --------------------------------------------------------------- expiry

def sweep() -> list[str]:
    """Drop everything past its date, plus blobs no index entry claims."""
    now = time.time()
    data = load()
    doomed = [fid for fid, f in data['files'].items() if now > f['expires']]
    for fid in doomed:
        delete_file(fid)

    known = set(load()['files'])
    for stray in BLOB_DIR.iterdir():
        if stray.is_file() and stray.name not in known:
            stray.unlink(missing_ok=True)

    with locked():
        data = load()
        stale = now - DEFAULT_DAYS * 86400
        empty = [t for t, e in data['bundles'].items()
                 if not any(f in data['files'] for f in e['files'])
                 and e.get('created', 0) < stale]
        for t in empty:
            data['bundles'].pop(t, None)
        if empty:
            save(data)
    return doomed


def stats() -> dict:
    data = load()
    total = sum(f['size'] for f in data['files'].values())
    return {'files': len(data['files']), 'bytes': total, 'free': free_space()}


def migrate() -> int:
    """Adopt files from the earlier flat layout, once."""
    legacy = DATA_DIR / 'incoming'
    if not legacy.is_dir():
        return 0
    stamp = re.compile(r'^\d{4}-\d{2}-\d{2}_\d{6}_(?:\d+_)?')
    moved = 0
    for old in sorted(legacy.iterdir()):
        if not old.is_file():
            continue
        entry = add_file(stamp.sub('', old.name) or old.name, old.stat().st_size)
        shutil.move(str(old), blob(entry['id']))
        moved += 1
    if moved:
        shutil.rmtree(legacy, ignore_errors=True)
    return moved
