"""Deciding how a file should be shown, and making the small picture.

Thumbnails and browser-safe renditions are best effort. If Pillow or
ffmpeg is missing or chokes on a file, the page falls back to a download
card rather than breaking.
"""
from __future__ import annotations

import json
import mimetypes
import shutil
import subprocess
from pathlib import Path

import db

THUMB_MAX = 640
PREVIEW_MAX = 2048

NATIVE_IMAGE = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif', '.svg'}
IMAGE = NATIVE_IMAGE | {'.heic', '.heif', '.bmp', '.tif', '.tiff', '.ico'}
VIDEO = {'.mp4', '.m4v', '.mov', '.webm', '.mkv', '.avi', '.mpg', '.mpeg', '.3gp', '.ogv'}
AUDIO = {'.mp3', '.m4a', '.aac', '.wav', '.flac', '.ogg', '.oga', '.opus', '.wma'}
TEXT = {'.txt', '.md', '.markdown', '.csv', '.tsv', '.json', '.xml', '.yml', '.yaml',
        '.log', '.ini', '.conf', '.py', '.js', '.ts', '.html', '.css', '.sh', '.sql',
        '.c', '.h', '.cpp', '.rs', '.go', '.java', '.rb', '.php', '.toml'}
# Containers a browser will not decode, however much we would like it to.
AWKWARD_VIDEO = {'.mkv', '.avi', '.mpg', '.mpeg', '.wmv', '.flv'}


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
    return special.get(ext) or mimetypes.guess_type(name)[0] or 'application/octet-stream'


def playable(name: str) -> bool:
    return Path(name).suffix.lower() not in AWKWARD_VIDEO


def _run(cmd: list[str], timeout: int = 90) -> bool:
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=timeout).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _shrink(src: Path, dst: Path, box: int, name: str) -> bool:
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
        # The blob has no extension, so ffmpeg needs the format spelled out.
        ext = Path(name).suffix.lstrip('.').lower() or 'jpeg'
        return _run(['ffmpeg', '-y', '-f', ext, '-i', str(src), '-vf',
                     f'scale={box}:{box}:force_original_aspect_ratio=decrease',
                     '-frames:v', '1', str(dst)])


def _frame(src: Path, dst: Path, box: int) -> bool:
    return _run(['ffmpeg', '-y', '-ss', '1', '-i', str(src), '-frames:v', '1',
                 '-vf', f'scale={box}:{box}:force_original_aspect_ratio=decrease',
                 str(dst)])


def thumbnail(file_id: str, name: str) -> Path | None:
    db.ensure_dirs()
    src = db.blob(file_id)
    if not src.is_file():
        return None
    dst = db.THUMB_DIR / f'{file_id}.jpg'
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    kind = kind_of(name)
    made = (_shrink(src, dst, THUMB_MAX, name) if kind == 'image'
            else _frame(src, dst, THUMB_MAX) if kind == 'video' else False)
    return dst if made and dst.exists() else None


def preview(file_id: str, name: str) -> Path | None:
    """What an <img> can actually paint. HEIC becomes JPEG here."""
    src = db.blob(file_id)
    if not src.is_file():
        return None
    if Path(name).suffix.lower() in NATIVE_IMAGE:
        return src
    db.ensure_dirs()
    dst = db.THUMB_DIR / f'{file_id}.preview.jpg'
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    return dst if _shrink(src, dst, PREVIEW_MAX, name) and dst.exists() else None


def probe(file_id: str, name: str) -> dict:
    info: dict = {}
    src = db.blob(file_id)
    kind = kind_of(name)
    if kind == 'image':
        try:
            from PIL import Image  # noqa: PLC0415
            try:
                import pillow_heif  # noqa: PLC0415
                pillow_heif.register_heif_opener()
            except ImportError:
                pass
            with Image.open(src) as im:
                info['width'], info['height'] = im.size
        except Exception:
            pass
    elif kind in ('video', 'audio') and shutil.which('ffprobe'):
        try:
            out = subprocess.run(['ffprobe', '-v', 'quiet', '-print_format', 'json',
                                  '-show_format', '-show_streams', str(src)],
                                 capture_output=True, text=True, timeout=30).stdout
            data = json.loads(out)
            if data.get('format', {}).get('duration'):
                info['duration'] = float(data['format']['duration'])
            for s in data.get('streams', []):
                if s.get('codec_type') == 'video':
                    info['width'], info['height'] = s.get('width'), s.get('height')
                    break
        except Exception:
            pass
    return info
