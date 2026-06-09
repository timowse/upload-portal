#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(description='Update upload portal config.json')
    p.add_argument('--config', default='config.json')
    p.add_argument('--backend-url', required=True)
    p.add_argument('--title', default='Upload Portal')
    p.add_argument('--lede', default='Bereit für einen direkten, sauberen Upload-Link.')
    p.add_argument('--summary', default='Upload ist freigeschaltet.')
    p.add_argument('--multi-file', action='store_true')
    p.add_argument('--max-files', type=int, default=1)
    p.add_argument('--no-auto-close', action='store_true')
    p.add_argument('--path', default='/')
    args = p.parse_args()

    path = Path(args.config)
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    data.update({
        'active': True,
        'title': args.title,
        'lede': args.lede,
        'summary': args.summary,
        'backendUrl': args.backend_url,
        'method': 'POST',
        'multiFile': args.multi_file,
        'maxFiles': args.max_files,
        'autoClose': not args.no_auto_close,
        'path': args.path,
        'label': 'aktiv',
        'options': [
            ['Modus', 'mehrere Dateien' if args.multi_file else 'eine Datei'],
            ['Max. Uploads', str(args.max_files)],
            ['Auto-Close', 'ja' if not args.no_auto_close else 'nein'],
            ['Backend', 'konfiguriert'],
        ],
    })
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
