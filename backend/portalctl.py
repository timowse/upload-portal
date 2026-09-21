#!/usr/bin/env python3
"""Housekeeping for the drop.

Uploading and sharing happen in the browser; nothing here is needed for
day to day use. This is for looking at what is stored and throwing
something out early.

    docker compose exec upload-portal python portalctl.py list
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import db
import media

PUBLIC_BASE = os.environ.get('PORTAL_PUBLIC_BASE', 'https://up.t1mo.dev').rstrip('/')


def human(n: int) -> str:
    step = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if step < 1024 or unit == 'TB':
            return f'{step:.0f} {unit}' if unit == 'B' else f'{step:.1f} {unit}'
        step /= 1024
    return f'{n} B'


def main() -> int:
    p = argparse.ArgumentParser(description='Upload-Portal verwalten')
    sub = p.add_subparsers(dest='cmd', required=True)

    ls = sub.add_parser('list', help='gespeicherte Dateien auflisten')
    ls.add_argument('--links', action='store_true', help='auch die Links ausgeben')

    rm = sub.add_parser('rm', help='eine Datei sofort loeschen')
    rm.add_argument('id', help='die ID aus "list"')

    sub.add_parser('sweep', help='abgelaufene Dateien jetzt entfernen')
    sub.add_parser('stats', help='Belegung anzeigen')

    args = p.parse_args()

    if args.cmd == 'list':
        files = db.list_files()
        if not files:
            print('(noch nichts gespeichert)')
            return 0
        now = time.time()
        for f in files:
            left = max(0, round((f['expires'] - now) / 86400))
            kind = media.kind_of(f['name'])
            print(f"{f['id']:14} {human(f['size']):>10}  {kind:6} "
                  f"noch {left:>3} Tage  {f['name'][:48]}")
            if args.links:
                print(f"{'':14} {PUBLIC_BASE}/s/{f['share']}")

    elif args.cmd == 'rm':
        if db.delete_file(args.id):
            print('geloescht')
        else:
            print('unbekannte ID', file=sys.stderr)
            return 1

    elif args.cmd == 'sweep':
        gone = db.sweep()
        print(f'{len(gone)} abgelaufene Dateien entfernt')

    elif args.cmd == 'stats':
        s = db.stats()
        print(f"Dateien : {s['files']}")
        print(f"Belegt  : {human(s['bytes'])}")
        print(f"Frei    : {human(s['free'])}")
        print(f"Ablauf  : {db.DEFAULT_DAYS} Tage")
        print(f"Max.    : {human(db.MAX_BYTES)} pro Datei")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
