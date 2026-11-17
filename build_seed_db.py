#!/usr/bin/env python3
"""Build the offline database that ships inside the app (assets/phonecheck_seed.db).

The app copies this file on first launch, so identification works instantly and fully offline,
with no server and no slow JSON parsing on the phone.

Examples
  # 1) Osmocom TAC data (download tacdb.json / tacdb.csv / tacdb.sqlite3 from http://tacdb.osmocom.org/)
  python3 tools/build_seed_db.py --tacdb ~/Downloads/tacdb.json --out assets/phonecheck_seed.db

  # 2) Osmocom data + your own specs exported from the backend (php tools/export_seed.php > payload.json)
  python3 tools/build_seed_db.py --tacdb tacdb.json --payload payload.json --out assets/phonecheck_seed.db

  # 3) Only a backend export (no Osmocom data)
  python3 tools/build_seed_db.py --payload payload.json --out assets/phonecheck_seed.db

  # 4) Osmocom + a community CSV with columns Brand,TAC,SPECS (e.g. github.com/MoazEb/tac-database tac_full.csv)
  python3 tools/build_seed_db.py --tacdb tacdb.json --community-csv tac_full.csv --out assets/phonecheck_seed.db
  #    Osmocom wins whenever both have the same TAC. Each source is stored separately with its own attribution.

What it does with --tacdb: one identifier row per TAC, plus one device row per brand+model so every
model is searchable in the Specs tab (specifications can be added later). It prints what it understood
(structure, row counts, sample rows, top brands). Check that output before building the app.

LICENSE: the Osmocom database is CC BY-SA 3.0. The attribution is stored in the database and shown
in the app (More > Data & updates). Keep it there.
"""
import argparse
import collections
import csv
import datetime
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

# Keep in sync with lib/core/db.dart (_onCreate).
SCHEMA = [
    'CREATE TABLE data_sources(id INTEGER PRIMARY KEY, name TEXT, url TEXT, license TEXT, attribution TEXT)',
    'CREATE TABLE brands(id INTEGER PRIMARY KEY, name TEXT)',
    'CREATE TABLE devices(id INTEGER PRIMARY KEY, brand_id INTEGER, name TEXT, released TEXT, status TEXT, image_url TEXT)',
    'CREATE TABLE device_variants(id INTEGER PRIMARY KEY, device_id INTEGER, model_number TEXT, region TEXT, sim_config TEXT, notes TEXT)',
    'CREATE TABLE device_specs(id INTEGER PRIMARY KEY, device_id INTEGER, variant_id INTEGER, category TEXT, spec_key TEXT, spec_value TEXT, source_id INTEGER, verified_at TEXT, confidence TEXT)',
    'CREATE TABLE identifiers(id INTEGER PRIMARY KEY, tac TEXT, brand_name TEXT, model_name TEXT, device_id INTEGER, variant_id INTEGER, source_id INTEGER, confidence TEXT)',
    'CREATE TABLE brand_instructions(id INTEGER PRIMARY KEY, brand_id INTEGER, title TEXT, body TEXT, sort_order INTEGER)',
    'CREATE TABLE saved_devices(device_id INTEGER PRIMARY KEY, saved_at TEXT)',
    'CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)',
    'CREATE INDEX idx_ident_tac ON identifiers(tac)',
    'CREATE INDEX idx_dev_name ON devices(name)',
    'CREATE INDEX idx_spec_dev ON device_specs(device_id)',
    'CREATE INDEX idx_var_dev ON device_variants(device_id)',
]
ENTITY_COLUMNS = {
    'data_sources': ['id', 'name', 'url', 'license', 'attribution'],
    'brands': ['id', 'name'],
    'devices': ['id', 'brand_id', 'name', 'released', 'status', 'image_url'],
    'device_variants': ['id', 'device_id', 'model_number', 'region', 'sim_config', 'notes'],
    'device_specs': ['id', 'device_id', 'variant_id', 'category', 'spec_key', 'spec_value', 'source_id', 'verified_at', 'confidence'],
    'identifiers': ['id', 'tac', 'brand_name', 'model_name', 'device_id', 'variant_id', 'source_id', 'confidence'],
    'brand_instructions': ['id', 'brand_id', 'title', 'body', 'sort_order'],
}

TAC_KEYS = ('tac', 'tac_code', 'tacs')
BRAND_KEYS = ('brand', 'brand_name', 'manufacturer', 'make', 'vendor')
MODEL_KEYS = ('model', 'model_name', 'marketing_name', 'marketing', 'product', 'device', 'name', 'name1')
MULTI_WORD_BRANDS = [
    'Sony Ericsson', 'Research In Motion', 'Nokia Siemens', 'Siemens Mobile', 'Sagem Wireless', 'Blu Products',
    'Alcatel Lucent', 'Huawei Honor', 'LG Electronics', 'HMD Global', 'Tecno Mobile', 'Infinix Mobility',
    'Lava International', 'Gionee Communication', 'Verizon Wireless',
]


# ------------------------------------------------------------------ reading
def iter_osmocom(data):
    """Osmocom tacdb.json: {"brands": {Brand: {"models": [{Model: {"tacs": [...], "alt_names": [...]}}]}}}.
    Models without any TAC are still yielded (tac empty) so they stay searchable."""
    for brand, binfo in data['brands'].items():
        models = binfo.get('models', []) if isinstance(binfo, dict) else []
        for entry in models:
            if isinstance(entry, str):
                entry = {entry: {}}
            if not isinstance(entry, dict):
                continue
            for model, minfo in entry.items():
                tacs = minfo.get('tacs', []) if isinstance(minfo, dict) else []
                if not tacs:
                    yield {'tac': '', 'brand': brand, 'model': model, '_notac': True}
                for tac in tacs:
                    yield {'tac': tac, 'brand': brand, 'model': model}


def iter_json(data):
    """Yield row dicts from the common JSON layouts (Osmocom nested, list of objects, dict keyed by TAC, wrapped list)."""
    if isinstance(data, dict) and isinstance(data.get('brands'), dict):
        yield from iter_osmocom(data)
        return
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                yield item
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                yield {'tac': item[0], 'name': item[1], 'brand': item[2] if len(item) > 2 else None}
        return
    if isinstance(data, dict):
        keys = list(data.keys())
        looks_keyed = keys and sum(1 for k in keys[:50] if re.fullmatch(r'\d{6,8}', str(k))) >= min(5, len(keys[:50]))
        if looks_keyed:
            for k, v in data.items():
                if isinstance(v, dict):
                    yield {'tac': k, **v}
                elif isinstance(v, str):
                    yield {'tac': k, 'name': v}
                elif isinstance(v, list) and v:
                    yield {'tac': k, 'name': v[0] if isinstance(v[0], str) else (v[0] or {}).get('name')}
            return
        # wrapped: use the largest container value
        containers = [v for v in data.values() if isinstance(v, (list, dict))]
        if containers:
            yield from iter_json(max(containers, key=len))


def load_rows(path: Path):
    ext = path.suffix.lower()
    if ext == '.json':
        with open(path, encoding='utf-8-sig') as f:
            data = json.load(f)
        return list(iter_json(data)), data
    if ext == '.csv':
        with open(path, encoding='utf-8-sig', newline='') as f:
            sample = f.read(4096)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
            except csv.Error:
                dialect = csv.excel
            rows = list(csv.DictReader(f, dialect=dialect))
        return rows, rows[:3]
    if ext in ('.sqlite3', '.sqlite', '.db'):
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tables:
            cols = [r[1].lower() for r in con.execute(f'PRAGMA table_info("{t}")')]
            if any(k in cols for k in TAC_KEYS):
                rows = [dict(r) for r in con.execute(f'SELECT * FROM "{t}"')]
                return rows, {'table': t, 'columns': cols}
        return [], {'tables': tables}
    sys.exit(f'Unsupported file type: {ext} (use .json, .csv or .sqlite3)')


def first(d: dict, keys):
    lower = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        v = lower.get(k)
        if isinstance(v, dict):
            v = v.get('name')
        if isinstance(v, list):
            v = v[0] if v else None
        if v is not None and str(v).strip():
            return str(v).strip()
    return ''


def split_brand(full: str):
    for b in MULTI_WORD_BRANDS:
        if full.lower().startswith(b.lower() + ' '):
            return b, full[len(b):].strip()
    parts = full.split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0], parts[0])


def normalize_row(d: dict):
    tac = re.sub(r'\D', '', first(d, TAC_KEYS))
    if not tac:
        if not d.get('_notac'):
            return None
        tac = None
    elif len(tac) > 8:
        return None
    else:
        tac = tac.zfill(8)
    brand = first(d, BRAND_KEYS)
    model = first(d, MODEL_KEYS)
    if not model and not brand:
        return None
    if not brand:
        brand, model = split_brand(model)
    elif model.lower().startswith(brand.lower() + ' '):
        model = model[len(brand):].strip()
    elif not model:
        model = brand
    brand = re.sub(r'\s+', ' ', brand)[:100]
    model = re.sub(r'\s+', ' ', model)[:150]
    if not brand or not model:
        return None
    return tac, brand, model


# ------------------------------------------------------------------ community CSV (Brand,TAC,SPECS)
BRAND_CASE = {
    'APPLE': 'Apple', 'SAMSUNG': 'Samsung', 'XIAOMI': 'Xiaomi', 'HUAWEI': 'Huawei', 'HONOR': 'HONOR',
    'OPPO': 'OPPO', 'VIVO': 'vivo', 'REALME': 'realme', 'TECNO': 'TECNO', 'INFINIX': 'Infinix', 'ITEL': 'itel',
    'ONEPLUS': 'OnePlus', 'GOOGLE': 'Google', 'NOKIA': 'Nokia', 'MOTOROLA': 'Motorola', 'POCO': 'POCO',
    'REDMI': 'Redmi', 'SONY': 'Sony', 'SONY ERICSSON': 'Sony Ericsson', 'ERICSSON': 'Ericsson', 'LENOVO': 'Lenovo',
    'ASUS': 'ASUS', 'ALCATEL': 'Alcatel', 'BLACKBERRY': 'BlackBerry', 'MICROSOFT': 'Microsoft', 'NOTHING': 'Nothing',
    'OUKITEL': 'Oukitel', 'TRANSSION': 'Transsion', 'MEIZU': 'Meizu', 'SAGEM': 'Sagem', 'SIEMENS': 'Siemens',
}
WORD_CASE = {'IPHONE': 'iPhone', 'IPAD': 'iPad', 'IPOD': 'iPod', 'AIRPODS': 'AirPods', 'MACBOOK': 'MacBook'}


def smart_title(text: str) -> str:
    """ALL CAPS -> readable case. Short tokens and tokens with digits stay upper (A22, 5G, XL)."""
    out = []
    for tok in text.split(' '):
        if not tok:
            continue
        if tok in WORD_CASE:
            out.append(WORD_CASE[tok])
        elif any(ch.isdigit() for ch in tok) or len(tok) <= 2:
            out.append(tok.upper())
        else:
            out.append(tok[:1].upper() + tok[1:].lower())
    return ' '.join(out)


def pretty_brand(raw: str, known: dict) -> str:
    k = re.sub(r'\s+', ' ', raw.replace('_', ' ')).strip()
    lk = k.lower()
    if lk in known:                      # keep spelling already used by an earlier source
        return known[lk]
    if k.upper() in BRAND_CASE:
        return BRAND_CASE[k.upper()]
    if len(k) <= 3:
        return k.upper()
    return smart_title(k) if k == k.upper() else k


def model_from_specs(specs: str, brand_raw: str):
    """First comma-separated part that is more than just the brand. Model numbers in later parts are
    glued to years ("SM-A226BR/DSN2021"), so they are not parsed."""
    b = brand_raw.strip().upper()
    parts = [p.strip() for p in re.split(r',\s+', specs or '') if p.strip()]
    for part in parts[:2]:
        up = part.upper()
        if up == b:
            continue
        if up.startswith(b + ' '):
            part = part[len(b) + 1:].strip()
        if part and part.upper() != b:
            part = re.sub(r'\s+', ' ', part)
            return smart_title(part) if part == part.upper() else part
    return None


def read_community_csv(path: Path):
    stats = collections.Counter()
    rows, seen = [], set()
    with open(path, encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            cols = {str(k).strip().lower(): v for k, v in r.items() if k}
            tac = re.sub(r'\D', '', (cols.get('tac') or '').strip())
            brand = (cols.get('brand') or '').strip()
            specs = (cols.get('specs') or cols.get('name') or cols.get('model') or '').strip()
            repaired = False
            if len(tac) == 7:           # a leading zero was lost (all such TACs start with 1: 01xxxxxx)
                tac, repaired = '0' + tac, True
            if len(tac) != 8:
                stats['skipped_bad_tac'] += 1
                continue
            model = model_from_specs(specs, brand)
            if not brand or not model:
                stats['skipped_no_name'] += 1
                continue
            if tac in seen:
                stats['skipped_duplicate_tac'] += 1
                continue
            seen.add(tac)
            if repaired:
                stats['repaired_leading_zero'] += 1
            rows.append((tac, brand, model, repaired))
    return rows, stats


# ------------------------------------------------------------------ ids / hashing
def new_id(ctx, table, key=None):
    """Next sequential id. Small, repetitive ids compress far better than hashes (the downloadable update
    is about half the size). Ids can shift between data versions; the app therefore re-matches saved devices
    by brand and model name after an update, never by id. Inputs are sorted first, so builds are reproducible."""
    ctx['seq'][table] += 1
    return ctx['seq'][table]


def content_hash(con) -> str:
    h = hashlib.sha256()
    for q in ('SELECT tac, brand_name, model_name FROM identifiers ORDER BY tac, brand_name, model_name',
              'SELECT b.name, d.name FROM devices d JOIN brands b ON b.id = d.brand_id ORDER BY b.name, d.name',
              'SELECT d.name, s.category, s.spec_key, s.spec_value FROM device_specs s JOIN devices d ON d.id = s.device_id ORDER BY d.name, s.category, s.spec_key'):
        for row in con.execute(q):
            h.update(('\x1f'.join('' if v is None else str(v) for v in row) + '\x1e').encode('utf-8'))
    return h.hexdigest()


# ------------------------------------------------------------------ building
def ingest(con, entries, source_id, create_devices, ctx):
    """entries: (tac|None, brand, model). Adds brand/device rows and identifier rows."""
    brands, devices, seen = ctx['brands'], ctx['devices'], ctx['seen']
    added = 0
    for tac, b, m in entries:
        key = (tac, b.lower(), m.lower())
        if key in seen:
            continue
        seen.add(key)
        device_id = None
        if create_devices:
            bid = brands.get(b.lower())
            if bid is None:
                bid = new_id(ctx, 'brands', 'b|' + b.lower())
                con.execute('INSERT INTO brands(id, name) VALUES(?,?)', (bid, b))
                brands[b.lower()] = bid
            did = devices.get((bid, m.lower()))
            if did is None:
                did = new_id(ctx, 'devices', f'd|{bid}|{m.lower()}')
                con.execute('INSERT INTO devices(id, brand_id, name) VALUES(?,?,?)', (did, bid, m))
                devices[(bid, m.lower())] = did
            device_id = did
        if tac is not None:
            iid = new_id(ctx, 'identifiers', f'i|{tac}|{b.lower()}|{m.lower()}')
            con.execute(
                'INSERT INTO identifiers(id, tac, brand_name, model_name, device_id, variant_id, source_id, confidence) '
                'VALUES(?,?,?,?,?,?,?,?)', (iid, tac, b, m, device_id, None, source_id, 'unconfirmed'))
            added += 1
    return added


def add_source(con, name, url, license_, attribution):
    return con.execute('INSERT INTO data_sources(name, url, license, attribution) VALUES(?,?,?,?)',
                       (name, url, license_, attribution)).lastrowid


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--tacdb', help='Osmocom export: tacdb.json, tacdb.csv or tacdb.sqlite3')
    ap.add_argument('--community-csv', help='CSV with columns Brand,TAC,SPECS (merged after Osmocom; Osmocom wins on conflicts)')
    ap.add_argument('--payload', help='Sync payload JSON from backend: php tools/export_seed.php')
    ap.add_argument('--out', default='assets/phonecheck_seed.db')
    ap.add_argument('--no-devices', action='store_true', help='do not create a device row per brand+model')
    ap.add_argument('--source-name', default='Osmocom TAC database')
    ap.add_argument('--build-version', type=int, help='data version, YYYYMMDD (default: today, UTC)')
    ap.add_argument('--version-file', help='where to write the version number (default: next to --out, .version)')
    args = ap.parse_args()
    if not (args.tacdb or args.payload or args.community_csv):
        ap.error('give --tacdb, --community-csv and/or --payload')

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    con = sqlite3.connect(out)
    for stmt in SCHEMA:
        con.execute(stmt)

    version, dataset_id = 1, None
    if args.payload:
        payload = json.load(open(args.payload, encoding='utf-8'))
        for table, cols in ENTITY_COLUMNS.items():
            rows = payload.get('changes', {}).get(table, {}).get('upserts', [])
            if rows:
                con.executemany(
                    f'INSERT OR REPLACE INTO {table}({",".join(cols)}) VALUES({",".join("?" * len(cols))})',
                    [[r.get(c) for c in cols] for r in rows])
        version = int(payload.get('version', 1))
        dataset_id = payload.get('dataset_id')
        print(f'Loaded payload: version {version}, ' +
              ', '.join(f'{t}={len(c.get("upserts", []))}' for t, c in payload.get('changes', {}).items()))

    ctx = {
        'brands': {r[1].lower(): r[0] for r in con.execute('SELECT id, name FROM brands')},
        'devices': {(r[0], r[1].lower()): r[2] for r in con.execute('SELECT brand_id, name, id FROM devices')},
        'seen': set(),
        'seq': {t: (con.execute(f'SELECT MAX(id) FROM {t}').fetchone()[0] or 0) for t in ('brands', 'devices', 'identifiers')},
    }
    create_devices = not args.no_devices
    bundled = False

    # ---------------------------------------------------------------- Osmocom
    if args.tacdb:
        path = Path(args.tacdb)
        raw_rows, raw = load_rows(path)
        print(f'\nRead {path.name}: {len(raw_rows)} raw rows')
        parsed, skipped = [], 0
        for r in raw_rows:
            n = normalize_row(r)
            if n:
                parsed.append(n)
            else:
                skipped += 1
        if not parsed:
            print('\nCould not understand this file. Structure sample (send me this):')
            print(json.dumps(raw, ensure_ascii=False, default=str)[:900])
            sys.exit(1)
        spell = collections.defaultdict(collections.Counter)
        for _, b, _ in parsed:
            spell[b.lower()][b] += 1
        brand_name = {k: c.most_common(1)[0][0] for k, c in spell.items()}
        source_id = add_source(
            con, args.source_name, 'http://tacdb.osmocom.org/', 'CC BY-SA 3.0',
            'Contains data from the Osmocom TAC database (tacdb.osmocom.org), licensed CC BY-SA 3.0. '
            'Names were normalized and may be incomplete.')
        osmo_entries = sorted(((t, brand_name[b.lower()], m) for t, b, m in parsed),
                              key=lambda e: (e[1].lower(), e[2].lower(), e[0] or ''))
        n_ident = ingest(con, osmo_entries, source_id, create_devices, ctx)
        print(f'Osmocom: {len(parsed)} rows ({skipped} skipped): {n_ident} TAC entries')
        print('Sample:')
        for r in con.execute('SELECT tac, brand_name, model_name FROM identifiers LIMIT 5'):
            print(f'  {r[0]}  {r[1]} | {r[2]}')
        bundled = True

    # ---------------------------------------------------------------- community CSV
    if args.community_csv:
        path = Path(args.community_csv)
        rows, stats = read_community_csv(path)
        osmo = {}
        for tac, brand in con.execute('SELECT tac, lower(brand_name) FROM identifiers'):
            osmo.setdefault(tac, set()).add(brand)
        known = {k: v for k, v in ((r[0].lower(), r[0]) for r in con.execute('SELECT name FROM brands'))}
        entries, overlap = [], 0
        zero_checked = zero_match = 0
        for tac, brand_raw, model, repaired in rows:
            brand = pretty_brand(brand_raw, known)
            known.setdefault(brand.lower(), brand)
            if tac in osmo:
                overlap += 1
                if repaired:
                    zero_checked += 1
                    zero_match += brand.lower() in osmo[tac]
                continue                      # earlier (Osmocom) source wins
            entries.append((tac, brand, model))
        source_id = add_source(
            con, 'Community TAC list (MoazEb/tac-database)', 'https://github.com/MoazEb/tac-database',
            'MIT (repository). Underlying sources vary',
            'Includes TAC data compiled in github.com/MoazEb/tac-database, which cites Osmocom, a public carrier '
            'TAC list and other sources. Names were normalized. Entries are unverified and may be wrong.')
        entries.sort(key=lambda e: (e[1].lower(), e[2].lower(), e[0]))
        n_ident = ingest(con, entries, source_id, create_devices, ctx)
        print(f'\nCommunity CSV {path.name}: {len(rows)} usable rows, {overlap} already in Osmocom (kept Osmocom), '
              f'{n_ident} TAC entries added')
        print('  skipped: ' + ', '.join(f'{k.replace("skipped_", "").replace("_", " ")}={v}'
                                         for k, v in stats.items() if k.startswith('skipped_')))
        if stats['repaired_leading_zero']:
            msg = f'  leading-zero repair: {stats["repaired_leading_zero"]} TACs restored (7 -> 8 digits)'
            if zero_checked:
                msg += f'; {zero_match} of {zero_checked} that Osmocom also lists have the same brand'
            print(msg)
        bundled = True

    if bundled:
        n_dev = con.execute('SELECT COUNT(*) FROM devices').fetchone()[0]
        n_id = con.execute('SELECT COUNT(*) FROM identifiers').fetchone()[0]
        print(f'\nTotal: {n_id} TAC entries, {n_dev} device models')
        top = con.execute('SELECT brand_name, COUNT(*) c FROM identifiers GROUP BY brand_name ORDER BY c DESC LIMIT 10').fetchall()
        print('Top brands:', ', '.join(f'{b} ({n})' for b, n in top))
        # TAC rows exist only in this file, never on a server: use a different dataset id so that
        # a later server sync starts from the server's own data instead of mixing ids.
        dataset_id = 'bundled'
        version = 1

    build_version = args.build_version or int(datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d'))
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('bundle_version', ?)", (str(build_version),))
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('schema_version', '1')")
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('content_hash', ?)", (content_hash(con),))
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('dataset_version', ?)", (str(version),))
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('dataset_id', ?)", (dataset_id or 'bundled',))
    con.execute('PRAGMA user_version = 1')
    con.commit()
    con.execute('VACUUM')
    con.close()
    vfile = Path(args.version_file) if args.version_file else out.with_suffix('.version')
    vfile.write_text(f'{build_version}\n')
    print(f'\nWrote {out} ({out.stat().st_size / 1_048_576:.1f} MB), data version {build_version}, version file {vfile}')


if __name__ == '__main__':
    main()
