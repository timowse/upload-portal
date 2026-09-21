"""Password gate for the admin interface.

The dashboard can upload, delete and publish files, so it needs a lock.
A password set through the environment plus a signed cookie is enough
here: one person, one device, an interface that is only reachable over
HTTPS through the tunnel.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

import store

COOKIE = 'portal_admin'
MAX_AGE = 30 * 86400          # stay signed in for a month
WINDOW = 300                  # rate limit window for failed attempts
MAX_TRIES = 8

PASSWORD = os.environ.get('PORTAL_ADMIN_PASSWORD', '').strip()
_attempts: list[float] = []


def enabled() -> bool:
    """The dashboard stays off until a password is configured."""
    return len(PASSWORD) >= 8


def _secret() -> bytes:
    """A signing key that survives restarts, created on first use."""
    path = store.DATA_DIR / '.cookie-secret'
    store.ensure_dirs()
    if not path.exists():
        path.write_bytes(secrets.token_bytes(32))
        path.chmod(0o600)
    return path.read_bytes()


def _sign(payload: str) -> str:
    mac = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip('=')


def issue() -> str:
    expires = int(time.time()) + MAX_AGE
    payload = str(expires)
    return f'{payload}.{_sign(payload)}'


def valid(token: str | None) -> bool:
    if not token or '.' not in token:
        return False
    payload, _, sig = token.rpartition('.')
    if not hmac.compare_digest(sig, _sign(payload)):
        return False
    try:
        return int(payload) > time.time()
    except ValueError:
        return False


def throttled() -> bool:
    """Drop the oldest attempts, then say whether too many remain."""
    now = time.time()
    _attempts[:] = [t for t in _attempts if now - t < WINDOW]
    return len(_attempts) >= MAX_TRIES


def check_password(candidate: str) -> bool:
    if not enabled():
        return False
    ok = hmac.compare_digest(PASSWORD, (candidate or '').strip())
    if not ok:
        _attempts.append(time.time())
    else:
        _attempts.clear()
    return ok
