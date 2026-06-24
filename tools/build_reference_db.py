#!/usr/bin/env python3
"""
Baut die statische AeroX-Referenz-DB (`data/aerox_reference.sqlite`) aus freien
Bulk-Datensätzen und legt sie gzip-komprimiert ins Repo. Die DB wird in das
Docker-Image gebacken (read-only) und beim Boot nach /tmp entpackt — sie
überlebt damit Cloud-Runs ephemeren Datenträger, weil sie im Image steckt.

Quellen (alle frei, ohne Key):
  - OurAirports      airports.csv    → airports     (~80k)
  - OpenFlights      airlines.dat    → airlines     (~6k)
  - OpenFlights      routes.dat      → routes       (~67k Seed)
  - OpenSky          aircraftDatabase.csv → aircraft (~500k, inkl. Baujahr)
  - rikgale ICAOList ICAOList.csv    → aircraft_types (~2.7k)

Lauf:  python3 tools/build_reference_db.py
Danach: git add data/aerox_reference.sqlite.gz && commit.

Self-growing-Teil (neue Hexes, echte Schedule-Zeiten) liegt NICHT hier, sondern
wandert zur Laufzeit in Supabase (siehe blueprints/aerox_data_blueprint.py).
"""
import csv
import gzip
import io
import os
import shutil
import sqlite3
import sys
import urllib.request

csv.field_size_limit(10_000_000)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DATA_DIR = os.path.join(REPO, 'data')
OUT_SQLITE = os.path.join(DATA_DIR, 'aerox_reference.sqlite')
OUT_GZ = OUT_SQLITE + '.gz'

SOURCES = {
    'airports': 'https://davidmegginson.github.io/ourairports-data/airports.csv',
    'airlines': 'https://raw.githubusercontent.com/jpatokal/openflights/master/data/airlines.dat',
    'routes':   'https://raw.githubusercontent.com/jpatokal/openflights/master/data/routes.dat',
    'aircraft': 'https://opensky-network.org/datasets/metadata/aircraftDatabase.csv',
    'types':    'https://raw.githubusercontent.com/rikgale/ICAOList/main/ICAOList.csv',
}


def _fetch(url):
    print(f'  ↓ {url}', flush=True)
    req = urllib.request.Request(url, headers={'User-Agent': 'AeroX-DataEngine/1.0'})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def _rows(raw, header=True):
    text = raw.decode('utf-8', errors='replace')
    rdr = csv.reader(io.StringIO(text))
    rows = list(rdr)
    return rows[1:] if header else rows


def _nz(v):
    """OpenFlights/OpenSky null-Marker → None."""
    if v is None:
        return None
    v = v.strip()
    return None if v in ('', '\\N', 'N/A', 'NULL') else v


def build():
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(OUT_SQLITE):
        os.remove(OUT_SQLITE)
    db = sqlite3.connect(OUT_SQLITE)
    db.executescript('''
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        CREATE TABLE airports (
            icao TEXT, iata TEXT, name TEXT, city TEXT, country TEXT,
            lat REAL, lon REAL, elev_ft INTEGER, region TEXT, type TEXT);
        CREATE TABLE airlines (
            iata TEXT, icao TEXT, name TEXT, callsign TEXT, country TEXT, active TEXT);
        CREATE TABLE aircraft (
            hex TEXT PRIMARY KEY, reg TEXT, typecode TEXT, manufacturer TEXT,
            model TEXT, operator TEXT, owner TEXT, built INTEGER, built_date TEXT, category TEXT);
        CREATE TABLE aircraft_types (
            typecode TEXT PRIMARY KEY, class TEXT, engines TEXT,
            manufacturer TEXT, model TEXT, name TEXT);
        CREATE TABLE routes (airline TEXT, src TEXT, dst TEXT);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    ''')
    counts = {}

    # ---- Airports (OurAirports) ----
    print('airports …', flush=True)
    raw = _fetch(SOURCES['airports'])
    rows = _rows(raw, header=True)
    # Header-Index dynamisch (OurAirports ändert Spalten-Reihenfolge gelegentlich).
    hdr = _rows(raw, header=False)[0]
    ix = {name: i for i, name in enumerate(hdr)}
    def col(r, name):
        i = ix.get(name)
        return _nz(r[i]) if i is not None and i < len(r) else None
    batch = []
    for r in rows:
        iata = col(r, 'iata_code')
        icao = col(r, 'icao_code') or col(r, 'ident')
        typ = col(r, 'type')
        # Reine Heliports/geschlossene ohne Code überspringen — Rauschen.
        if not iata and not icao:
            continue
        try:
            lat = float(col(r, 'latitude_deg')) if col(r, 'latitude_deg') else None
            lon = float(col(r, 'longitude_deg')) if col(r, 'longitude_deg') else None
        except ValueError:
            lat = lon = None
        try:
            elev = int(float(col(r, 'elevation_ft'))) if col(r, 'elevation_ft') else None
        except ValueError:
            elev = None
        batch.append((icao, iata, col(r, 'name'), col(r, 'municipality'),
                      col(r, 'iso_country'), lat, lon, elev, col(r, 'iso_region'), typ))
    db.executemany('INSERT INTO airports VALUES (?,?,?,?,?,?,?,?,?,?)', batch)
    counts['airports'] = len(batch)

    # ---- Airlines (OpenFlights) ----
    print('airlines …', flush=True)
    rows = _rows(_fetch(SOURCES['airlines']), header=False)
    batch = []
    for r in rows:
        if len(r) < 8:
            continue
        # ID,Name,Alias,IATA,ICAO,Callsign,Country,Active
        iata, icao = _nz(r[3]), _nz(r[4])
        if not iata and not icao:
            continue
        batch.append((iata, icao, _nz(r[1]), _nz(r[5]), _nz(r[6]), _nz(r[7])))
    db.executemany('INSERT INTO airlines VALUES (?,?,?,?,?,?)', batch)
    counts['airlines'] = len(batch)

    # ---- Routes (OpenFlights, Seed) ----
    print('routes …', flush=True)
    rows = _rows(_fetch(SOURCES['routes']), header=False)
    batch = []
    for r in rows:
        if len(r) < 5:
            continue
        # Airline,AirlineID,Src,SrcID,Dst,DstID,...
        al, src, dst = _nz(r[0]), _nz(r[2]), _nz(r[4])
        if src and dst:
            batch.append((al, src, dst))
    db.executemany('INSERT INTO routes VALUES (?,?,?)', batch)
    counts['routes'] = len(batch)

    # ---- Aircraft types (rikgale ICAOList) ----
    print('aircraft_types …', flush=True)
    rows = _rows(_fetch(SOURCES['types']), header=True)
    batch, seen = [], set()
    for r in rows:
        if len(r) < 4:
            continue
        tc = _nz(r[0])
        if not tc or tc in seen:
            continue
        seen.add(tc)
        mm = (r[3] or '').split(',', 1)
        mfr = _nz(mm[0]) if mm else None
        model = _nz(mm[1]) if len(mm) > 1 else None
        name = ' '.join(x for x in [mfr, model] if x) or None
        batch.append((tc, _nz(r[1]), _nz(r[2]), mfr, model, name))
    db.executemany('INSERT INTO aircraft_types VALUES (?,?,?,?,?,?)', batch)
    counts['aircraft_types'] = len(batch)

    # ---- Aircraft registry (OpenSky, der große Brocken) ----
    print('aircraft (OpenSky, ~25 MB) …', flush=True)
    raw = _fetch(SOURCES['aircraft'])
    text = raw.decode('utf-8', errors='replace')
    rdr = csv.reader(io.StringIO(text))
    hdr = next(rdr)
    ix = {name: i for i, name in enumerate(hdr)}
    def acol(r, name):
        i = ix.get(name)
        return _nz(r[i]) if i is not None and i < len(r) else None
    batch, n = [], 0
    for r in rdr:
        hexid = acol(r, 'icao24')
        if not hexid:
            continue
        hexid = hexid.strip().lower()
        built = acol(r, 'built')
        year = None
        built_date = None
        if built:
            # Formate: "1998", "1998-03-12", "1998-03-12T..."
            try:
                year = int(built[:4])
                if year < 1930 or year > 2100:
                    year = None
                # Volles Datum (YYYY-MM-DD) mitnehmen → tagesgenaues Alter (User).
                elif len(built) >= 10 and built[4] == '-' and built[7] == '-':
                    built_date = built[:10]
            except ValueError:
                year = None
        batch.append((
            hexid, acol(r, 'registration'), acol(r, 'typecode'),
            acol(r, 'manufacturername') or acol(r, 'manufacturericao'),
            acol(r, 'model'), acol(r, 'operator'), acol(r, 'owner'),
            year, built_date, acol(r, 'categoryDescription')))
        if len(batch) >= 20000:
            db.executemany('INSERT OR IGNORE INTO aircraft VALUES (?,?,?,?,?,?,?,?,?,?)', batch)
            n += len(batch); batch = []
    if batch:
        db.executemany('INSERT OR IGNORE INTO aircraft VALUES (?,?,?,?,?,?,?,?,?,?)', batch)
        n += len(batch)
    counts['aircraft'] = n

    # ---- Indexe für schnelle Lookups ----
    print('indexing …', flush=True)
    db.executescript('''
        CREATE INDEX idx_ap_iata ON airports(iata);
        CREATE INDEX idx_ap_icao ON airports(icao);
        CREATE INDEX idx_al_iata ON airlines(iata);
        CREATE INDEX idx_al_icao ON airlines(icao);
        CREATE INDEX idx_ac_reg  ON aircraft(reg);
        CREATE INDEX idx_rt_air  ON routes(airline);
    ''')
    for k, v in counts.items():
        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (f'count_{k}', str(v)))
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
               ('built_by', 'tools/build_reference_db.py'))
    db.commit()
    db.execute('VACUUM')
    db.commit()
    db.close()

    raw_mb = os.path.getsize(OUT_SQLITE) / 1e6
    with open(OUT_SQLITE, 'rb') as f_in, gzip.open(OUT_GZ, 'wb', compresslevel=9) as f_out:
        shutil.copyfileobj(f_in, f_out)
    gz_mb = os.path.getsize(OUT_GZ) / 1e6
    # Die rohe .sqlite NICHT committen (nur .gz) — Repo schlank halten.
    os.remove(OUT_SQLITE)

    print('\n=== AeroX Reference DB gebaut ===')
    for k, v in counts.items():
        print(f'  {k:16} {v:>8,}')
    print(f'  sqlite {raw_mb:.1f} MB → gz {gz_mb:.1f} MB  ({OUT_GZ})')


if __name__ == '__main__':
    try:
        build()
    except Exception as e:
        print(f'FAIL: {type(e).__name__}: {e}', file=sys.stderr)
        sys.exit(1)
