"""Share links for files that already landed on the Pi.

A share is a short token pointing at one file in the incoming directory.
The Pi serves the viewer page itself rather than GitHub Pages, because a
messenger asking for a link preview only ever sees the server response -
it cannot run the page, and it never sends the URL fragment. Open Graph
tags therefore have to come from here.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import store

SHARES_FILE = store.DATA_DIR / 'shares.json'
THUMB_DIR = store.DATA_DIR / 'thumbs'
SAFE_TOKEN = re.compile(r'^[A-Za-z0-9_-]{8,64}$')

THUMB_MAX = 640          # preview card and poster frame
PREVIEW_MAX = 2048       # web-safe rendition of an image the browser cannot show

# Formats every current browser paints without help. Anything else that is
# still an image gets converted before it reaches the page.
NATIVE_IMAGE = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif', '.svg'}
IMAGE = NATIVE_IMAGE | {'.heic', '.heif', '.bmp', '.tif', '.tiff', '.ico'}
VIDEO = {'.mp4', '.m4v', '.mov', '.webm', '.mkv', '.avi', '.mpg', '.mpeg', '.3gp', '.ogv'}
AUDIO = {'.mp3', '.m4a', '.aac', '.wav', '.flac', '.ogg', '.oga', '.opus', '.wma'}
TEXT = {'.txt', '.md', '.markdown', '.csv', '.tsv', '.json', '.xml', '.yml', '.yaml',
        '.log', '.ini', '.conf', '.py', '.js', '.ts', '.html', '.css', '.sh', '.sql',
        '.c', '.h', '.cpp', '.rs', '.go', '.java', '.rb', '.php', '.toml'}

# A browser will not decode these even though they are video containers, so
# the page offers a download instead of a dead player.
AWKWARD_VIDEO = {'.mkv', '.avi', '.mpg', '.mpeg', '.wmv', '.flv'}


def ensure_dirs() -> None:
    store.ensure_dirs()
    THUMB_DIR.mkdir(parents=True, exist_ok=True)


def read() -> dict:
    try:
        return json.loads(SHARES_FILE.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write(data: dict) -> None:
    ensure_dirs()
    tmp = SHARES_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, SHARES_FILE)


STAMP = re.compile(r'^\d{4}-\d{2}-\d{2}_\d{6}_(?:\d+_)?')


def display_name(name: str) -> str:
    """Drop the timestamp we prefix on arrival.

    It exists so nothing overwrites anything, but whoever opens the link
    wants to see the file they were sent, not our bookkeeping.
    """
    return STAMP.sub('', name) or name


def kind_of(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in IMAGE:
        return 'image'
    if ext in VIDEO:
        return 'video'
    if ext in AUDIO:
        return 'audio'
    if ext == '.pdf':
        return 'pdf'
    if ext in TEXT:
        return 'text'
    return 'file'


def mime_of(name: str) -> str:
    ext = Path(name).suffix.lower()
    special = {'.heic': 'image/heic', '.heif': 'image/heif', '.mov': 'video/mp4',
               '.m4v': 'video/mp4', '.mkv': 'video/x-matroska', '.md': 'text/markdown',
               '.opus': 'audio/ogg', '.m4a': 'audio/mp4'}
    if ext in special:
        return special[ext]
    return mimetypes.guess_type(name)[0] or 'application/octet-stream'


def playable(name: str) -> bool:
    """Whether a browser has a realistic chance of decoding this itself."""
    return Path(name).suffix.lower() not in AWKWARD_VIDEO


def create(filename: str, days: int | None = None) -> dict:
    target = store.INCOMING_DIR / filename
    if not target.is_file():
        raise FileNotFoundError(filename)
    ensure_dirs()
    with store.locked():
        data = read()
        token = secrets.token_urlsafe(9)
        data[token] = {
            'file': filename,
            'created': int(time.time()),
            'expires': int(time.time() + days * 86400) if days else None,
            'views': 0,
        }
        write(data)
    return {'token': token, **data[token]}


def revoke(token: str) -> bool:
    with store.locked():
        data = read()
        gone = data.pop(token, None) is not None
        if gone:
            write(data)
    return gone


def resolve(token: str) -> tuple[dict, Path] | None:
    """Look up a live share and the file behind it."""
    if not SAFE_TOKEN.match(token or ''):
        return None
    entry = read().get(token)
    if not entry:
        return None
    if entry.get('expires') and time.time() > entry['expires']:
        return None
    path = store.INCOMING_DIR / entry['file']
    # The name came from our own directory listing, but re-check that it did
    # not escape it before opening anything.
    try:
        path.resolve().relative_to(store.INCOMING_DIR.resolve())
    except ValueError:
        return None
    if not path.is_file():
        return None
    return entry, path


def count_view(token: str) -> None:
    with store.locked():
        data = read()
        if token in data:
            data[token]['views'] = int(data[token].get('views', 0)) + 1
            write(data)


def _run(cmd: list[str], timeout: int = 60) -> bool:
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=timeout).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _shrink_image(src: Path, dst: Path, box: int) -> bool:
    """Best effort: Pillow first, ffmpeg as a fallback, give up quietly."""
    try:
        from PIL import Image  # noqa: PLC0415
        try:
            import pillow_heif  # noqa: PLC0415
            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        with Image.open(src) as im:
            im = im.convert('RGB')
            im.thumbnail((box, box), Image.LANCZOS)
            im.save(dst, 'JPEG', quality=82, optimize=True)
        return True
    except Exception:
        return _run(['ffmpeg', '-y', '-i', str(src), '-vf',
                     f'scale={box}:{box}:force_original_aspect_ratio=decrease',
                     '-frames:v', '1', str(dst)])


def _video_frame(src: Path, dst: Path, box: int) -> bool:
    return _run(['ffmpeg', '-y', '-ss', '1', '-i', str(src), '-frames:v', '1',
                 '-vf', f'scale={box}:{box}:force_original_aspect_ratio=decrease',
                 str(dst)])


def cache_key(path: Path) -> str:
    return hashlib.sha1(path.name.encode('utf-8')).hexdigest()[:20]


def thumbnail(path: Path) -> Path | None:
    """A small JPEG for the preview card and the video poster, cached on disk."""
    ensure_dirs()
    dst = THUMB_DIR / f'{cache_key(path)}.jpg'
    if dst.exists() and dst.stat().st_mtime >= path.stat().st_mtime:
        return dst
    kind = kind_of(path.name)
    made = _shrink_image(path, dst, THUMB_MAX) if kind == 'image' else (
        _video_frame(path, dst, THUMB_MAX) if kind == 'video' else False)
    return dst if made and dst.exists() else None


def preview_image(path: Path) -> Path | None:
    """A browser-safe rendition of an image that browsers cannot show directly."""
    if path.suffix.lower() in NATIVE_IMAGE:
        return path
    ensure_dirs()
    dst = THUMB_DIR / f'{cache_key(path)}.preview.jpg'
    if dst.exists() and dst.stat().st_mtime >= path.stat().st_mtime:
        return dst
    return dst if _shrink_image(path, dst, PREVIEW_MAX) and dst.exists() else None


def probe(path: Path) -> dict:
    """Pixel size and duration, when the tools to find them are available."""
    info: dict = {}
    kind = kind_of(path.name)
    if kind == 'image':
        try:
            from PIL import Image  # noqa: PLC0415
            try:
                import pillow_heif  # noqa: PLC0415
                pillow_heif.register_heif_opener()
            except ImportError:
                pass
            with Image.open(path) as im:
                info['width'], info['height'] = im.size
        except Exception:
            pass
    elif kind in ('video', 'audio') and shutil.which('ffprobe'):
        try:
            out = subprocess.run(
                ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format',
                 '-show_streams', str(path)],
                capture_output=True, text=True, timeout=30).stdout
            data = json.loads(out)
            dur = data.get('format', {}).get('duration')
            if dur:
                info['duration'] = float(dur)
            for s in data.get('streams', []):
                if s.get('codec_type') == 'video':
                    info['width'], info['height'] = s.get('width'), s.get('height')
                    break
        except Exception:
            pass
    return info
