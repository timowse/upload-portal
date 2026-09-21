"""Shared state for the upload portal.

The state lives in a single JSON file on the data volume. Both the HTTP
service and the command line tool read and write it through the helpers
here, so an flock around every read-modify-write is enough to keep them
from clobbering each other.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.environ.get('PORTAL_DATA', '/data'))
STATE_FILE = DATA_DIR / 'state.json'
LOCK_FILE = DATA_DIR / '.state.lock'
INCOMING_DIR = DATA_DIR / 'incoming'
TMP_DIR = DATA_DIR / 'tmp'

# Cloudflare caps a proxied request body at 100 MB on the free plan, and a
# tunnel is always proxied. Chunks stay well below that.
CHUNK_SIZE = 32 * 1024 * 1024

DEFAULTS = {
    'active': False,
    'token': None,
    'maxFiles': 1,
    'maxBytes': 4 * 1024 * 1024 * 1024,
    'autoClose': True,
    'uploaded': 0,
    'openedAt': None,
    'note': '',
}

SAFE_ID = re.compile(r'^[A-Za-z0-9_-]{6,64}$')


def ensure_dirs() -> None:
    for d in (DATA_DIR, INCOMING_DIR, TMP_DIR):
        d.mkdir(parents=True, exist_ok=True)


@contextmanager
def locked():
    """Hold an exclusive lock for the duration of a read-modify-write."""
    ensure_dirs()
    with open(LOCK_FILE, 'w') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def read() -> dict:
    ensure_dirs()
    try:
        data = json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    return {**DEFAULTS, **data}


def write(state: dict) -> None:
    ensure_dirs()
    tmp = STATE_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(state, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, STATE_FILE)


def new_token() -> str:
    return secrets.token_urlsafe(16)


def remaining(state: dict) -> int:
    return max(0, int(state['maxFiles']) - int(state['uploaded']))


def is_open(state: dict, token: str | None) -> bool:
    """A session is usable only for the exact token it was opened with."""
    if not state['active'] or not state['token'] or not token:
        return False
    if not secrets.compare_digest(str(state['token']), str(token)):
        return False
    return remaining(state) > 0


def open_session(max_files: int, max_bytes: int, auto_close: bool, note: str = '') -> dict:
    with locked():
        state = read()
        state.update({
            'active': True,
            'token': new_token(),
            'maxFiles': max_files,
            'maxBytes': max_bytes,
            'autoClose': auto_close,
            'uploaded': 0,
            'openedAt': int(time.time()),
            'note': note,
        })
        write(state)
        return state


def close_session() -> dict:
    with locked():
        state = read()
        state.update({'active': False, 'token': None})
        write(state)
        return state


def count_upload() -> dict:
    """Record one finished upload and close the session if that was the last."""
    with locked():
        state = read()
        state['uploaded'] = int(state['uploaded']) + 1
        if state['autoClose'] and remaining(state) <= 0:
            state['active'] = False
            state['token'] = None
        write(state)
        return state


def safe_filename(name: str) -> str:
    """Reduce a client supplied name to something safe to place on disk."""
    name = os.path.basename(str(name or '')).replace('\x00', '')
    name = re.sub(r'[^A-Za-z0-9._ \-()\[\]]+', '_', name).strip(' .')
    name = re.sub(r'_{2,}', '_', name)
    if not name:
        name = 'datei'
    return name[:120]


def unique_target(name: str) -> Path:
    """Timestamp every upload so a repeated filename never overwrites."""
    ensure_dirs()
    stamp = time.strftime('%Y-%m-%d_%H%M%S')
    target = INCOMING_DIR / f'{stamp}_{name}'
    counter = 2
    while target.exists():
        target = INCOMING_DIR / f'{stamp}_{counter}_{name}'
        counter += 1
    return target
