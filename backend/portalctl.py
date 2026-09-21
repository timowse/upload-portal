#!/usr/bin/env python3
"""Open and close upload sessions from the Pi.

Opening a session mints a fresh token and prints the link to hand out.
The token lives only in that link, so an old link stops working the
moment a new session is opened or the session closes itself.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

import store

LINK_BASE = os.environ.get('PORTAL_LINK_BASE', 'https://upload.t1mo.dev').rstrip('/')

UNITS = {'K': 1024, 'M': 1024 ** 2, 'G': 1024 ** 3, 'T': 1024 ** 4}


def parse_size(text: str) -> int:
    m = re.fullmatch(r'\s*(\d+(?:[.,]\d+)?)\s*([KMGT])?B?\s*', str(text), re.IGNORECASE)
    if not m:
        raise argparse.ArgumentTypeError(f'Groesse nicht lesbar: {text}')
    value = float(m.group(1).replace(',', '.'))
    return int(value * UNITS.get((m.group(2) or '').upper(), 1))


def human(n: int) -> str:
    step = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if step < 1024 or unit == 'TB':
            return f'{step:.0f} {unit}' if unit == 'B' else f'{step:.1f} {unit}'
        step /= 1024
    return f'{n} B'


def show(state: dict) -> None:
    if state['active'] and state['token']:
        print('Status      : OFFEN')
        print(f"Link        : {LINK_BASE}/#{state['token']}")
        print(f"Uploads     : {state['uploaded']} von {state['maxFiles']}"
              f" ({store.remaining(state)} frei)")
        print(f"Max. Groesse: {human(int(state['maxBytes']))} pro Datei")
        print(f"Auto-Close  : {'ja' if state['autoClose'] else 'nein'}")
        if state['note']:
            print(f"Notiz       : {state['note']}")
    else:
        print('Status      : GESCHLOSSEN')
        print(f"Zuletzt     : {state['uploaded']} von {state['maxFiles']} hochgeladen")


def main() -> int:
    p = argparse.ArgumentParser(description='Upload-Portal steuern')
    sub = p.add_subparsers(dest='cmd', required=True)

    op = sub.add_parser('open', help='Freigabe oeffnen und Link ausgeben')
    op.add_argument('--files', type=int, default=1, help='wie viele Dateien erlaubt sind')
    op.add_argument('--max-size', type=parse_size, default='4G', help='Limit pro Datei, z.B. 500M')
    op.add_argument('--no-auto-close', action='store_true', help='nach dem letzten Upload offen lassen')
    op.add_argument('--note', default='', help='Hinweis, der im Portal angezeigt wird')

    sub.add_parser('close', help='Freigabe sofort schliessen')
    sub.add_parser('status', help='aktuellen Zustand anzeigen')
    sub.add_parser('list', help='empfangene Dateien auflisten')

    args = p.parse_args()

    if args.cmd == 'open':
        if args.files < 1:
            print('--files muss mindestens 1 sein', file=sys.stderr)
            return 2
        show(store.open_session(args.files, args.max_size, not args.no_auto_close, args.note))
    elif args.cmd == 'close':
        show(store.close_session())
    elif args.cmd == 'status':
        show(store.read())
    elif args.cmd == 'list':
        store.ensure_dirs()
        files = sorted(store.INCOMING_DIR.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
        if not files:
            print('(noch nichts empfangen)')
        for f in files:
            print(f'{human(f.stat().st_size):>10}  {f.name}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
