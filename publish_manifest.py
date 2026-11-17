#!/usr/bin/env python3
"""Build the downloadable phone database and its manifest, with safety checks.

Run by the GitHub Actions workflow. Can also be run by hand:
  python3 publish_manifest.py --tacdb work/tacdb.json --community-csv work/tac_full.csv \
      --previous work/previous_manifest.json --repo OWNER/REPO --out dist

Exit code 0 and `changed=false`  -> nothing new, do not publish.
Exit code 0 and `changed=true`   -> publish dist/phonecheck_data.db.gz and dist/manifest.json.
Non-zero                         -> a check failed, publish NOTHING (the app keeps its current data).
"""
import argparse
import datetime
import gzip
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

MAX_GZ_BYTES = 60 * 1024 * 1024
HERE = Path(__file__).resolve().parent


def fail(msg):
    print(f'CHECK FAILED: {msg}', file=sys.stderr)
    sys.exit(1)


def set_output(key, value):
    path = os.environ.get('GITHUB_OUTPUT')
    if path:
        with open(path, 'a') as f:
            f.write(f'{key}={value}\n')
    print(f'[output] {key}={value}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tacdb', required=True)
    ap.add_argument('--community-csv')
    ap.add_argument('--previous', help='previous manifest.json (may be missing or {})')
    ap.add_argument('--repo', required=True, help='OWNER/REPO that hosts the release')
    ap.add_argument('--out', default='dist')
    ap.add_argument('--tag', default='data')
    ap.add_argument('--min-tac', type=int, default=50000, help='fail if fewer TAC entries than this')
    ap.add_argument('--max-drop', type=float, default=0.10, help='fail if TAC entries drop more than this fraction')
    ap.add_argument('--require-brands', default='Samsung:1000,Apple:500',
                    help='brand:min_count list that must be present, or empty to skip')
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    prev = {}
    if args.previous and Path(args.previous).exists():
        try:
            prev = json.loads(Path(args.previous).read_text() or '{}')
        except json.JSONDecodeError:
            prev = {}
    prev_version = int(prev.get('version', 0) or 0)

    today = int(datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d'))
    version = max(today, prev_version + 1)

    db = out / 'phonecheck_data.db'
    cmd = [sys.executable, str(HERE / 'build_seed_db.py'), '--tacdb', args.tacdb, '--out', str(db),
           '--build-version', str(version)]
    if args.community_csv:
        cmd += ['--community-csv', args.community_csv]
    print('Building:', ' '.join(cmd))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        fail('database build failed (the source file format may have changed)')

    con = sqlite3.connect(db)
    if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        fail('database integrity check failed')
    tac = con.execute('SELECT COUNT(*) FROM identifiers').fetchone()[0]
    models = con.execute('SELECT COUNT(*) FROM devices').fetchone()[0]
    meta = dict(con.execute('SELECT k, v FROM meta').fetchall())
    if meta.get('schema_version') != '1':
        fail('unexpected schema version')

    if tac < args.min_tac:
        fail(f'only {tac} TAC entries (minimum {args.min_tac}). A source file may be empty or broken.')
    prev_tac = int((prev.get('counts') or {}).get('tac', 0) or 0)
    if prev_tac and tac < prev_tac * (1 - args.max_drop):
        fail(f'TAC entries dropped from {prev_tac} to {tac} (more than {int(args.max_drop * 100)}%). '
             'A source probably lost data. Nothing was published.')
    for item in filter(None, (x.strip() for x in args.require_brands.split(','))):
        brand, _, minimum = item.partition(':')
        n = con.execute('SELECT COUNT(*) FROM identifiers WHERE brand_name = ? COLLATE NOCASE', (brand,)).fetchone()[0]
        if n < int(minimum or 1):
            fail(f'brand {brand} has only {n} entries (expected at least {minimum})')
    con.close()

    chash = meta['content_hash']
    if prev.get('content_hash') == chash:
        print('No change in content since the last release. Not publishing.')
        set_output('changed', 'false')
        db.unlink(missing_ok=True)
        return

    gz = out / 'phonecheck_data.db.gz'
    with open(db, 'rb') as src, open(gz, 'wb') as raw:
        with gzip.GzipFile(filename='', mode='wb', fileobj=raw, compresslevel=9, mtime=0) as g:
            while chunk := src.read(1 << 20):
                g.write(chunk)
    db.unlink()
    size = gz.stat().st_size
    if size > MAX_GZ_BYTES:
        fail(f'compressed database is {size} bytes (limit {MAX_GZ_BYTES})')
    sha = hashlib.sha256(gz.read_bytes()).hexdigest()

    base = f'https://github.com/{args.repo}/releases/download/{args.tag}'
    manifest = {
        'schema': 1,
        'version': version,
        'published_at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'db_url': f'{base}/phonecheck_data.db.gz',
        'sha256': sha,
        'size_bytes': size,
        'counts': {'tac': tac, 'models': models},
        'content_hash': chash,
    }
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest, indent=2))
    set_output('changed', 'true')
    set_output('version', version)


if __name__ == '__main__':
    main()
