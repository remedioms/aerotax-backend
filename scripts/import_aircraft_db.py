#!/usr/bin/env python3
"""Import der OpenSky-Aircraft-Database in die Supabase-Tabelle `tail_hex`.

Befüllt die Reg→Hex-Stammdaten für den "Flieger nach Registration"-Tracker aus
der frei verfügbaren OpenSky-Aircraft-Database (CSV, ~hunderte MB). KEINE bezahlte
Flug-API. Idempotent — bei jedem Lauf wird per Registration ge-upsertet, sodass
ein erneuter Lauf die Tabelle auf den aktuellen Stand bringt statt zu duplizieren.

Datenquelle (Stand 2026-06):
    https://opensky-network.org/datasets/metadata/aircraftDatabase.csv

    Falls diese URL 404't (OpenSky hat das Layout in der Vergangenheit umgestellt),
    sind die aktuell gültigen Alternativen:
      · Übersichtsseite mit dem jeweils aktuellen Direktlink:
        https://opensky-network.org/datasets/metadata/
      · Versionierte Monatsstände (Beispiel-Muster):
        https://opensky-network.org/datasets/metadata/aircraft-database-complete-YYYY-MM.csv
    In dem Fall hier AIRCRAFT_DB_URL anpassen (oder via Env AIRCRAFT_DB_URL=...).

CSV-Spalten (OpenSky): icao24, registration, manufacturericao, manufacturername,
    model, typecode, serialnumber, linenumber, icaoaircrafttype, operator,
    operatorcallsign, operatoricao, operatoriata, owner, testreg, registered,
    reguntil, status, built, firstflightdate, seatconfiguration, engines,
    modes, adsb, acars, notes, categoryDescription
Wir nutzen: icao24, registration, typecode, operator.

STREAMING: Die Datei wird NICHT komplett in den Speicher geladen. Wir streamen
den HTTP-Response zeilenweise durch csv.reader und upserten in Batches.

Aufruf (manuell jetzt, danach monatlich refreshen — manuell oder via Cron):
    SUPABASE_URL=https://xxx.supabase.co \
    SUPABASE_SERVICE_KEY=eyJ... \
    python3 scripts/import_aircraft_db.py

Optionen (Env):
    AIRCRAFT_DB_URL   — überschreibt die CSV-URL (z.B. neuer Monatsstand)
    TAIL_HEX_BATCH    — Batch-Größe (default 500)
    AIRCRAFT_DB_LIMIT — nur die ersten N gültigen Rows importieren (Debug/Smoke-Test)
"""

import csv
import io
import os
import sys
import urllib.request
import urllib.error

AIRCRAFT_DB_URL = os.environ.get(
    'AIRCRAFT_DB_URL',
    'https://opensky-network.org/datasets/metadata/aircraftDatabase.csv',
)
BATCH_SIZE = int(os.environ.get('TAIL_HEX_BATCH', '500'))
ROW_LIMIT = int(os.environ.get('AIRCRAFT_DB_LIMIT', '0'))  # 0 = kein Limit
USER_AGENT = 'AeroTax-Backend/1.1 (tail_hex-importer; mailto:ops@aerotax.de)'


def _make_supabase_client():
    """Service-Client identisch zur app.py-Init (SUPABASE_URL/SUPABASE_SERVICE_KEY).
    Service-Role-Key umgeht RLS — Pflicht für den Upsert in tail_hex."""
    try:
        from supabase import create_client
    except ImportError:
        sys.exit("FEHLER: 'supabase' nicht installiert (pip install supabase).")
    url = os.environ.get('SUPABASE_URL', '')
    key = os.environ.get('SUPABASE_SERVICE_KEY', '')
    if not url or not key:
        sys.exit("FEHLER: SUPABASE_URL und SUPABASE_SERVICE_KEY müssen gesetzt sein.")
    return create_client(url, key)


def _clean(v):
    """Trim + None bei leer. OpenSky füllt fehlende Felder mit leerem String."""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _flush(sb, batch):
    """Upsert eines Batches in tail_hex (on_conflict=registration). Returns die
    Anzahl der geschriebenen Rows. Wirft NICHT — Fehler werden geloggt und der
    Lauf macht mit dem nächsten Batch weiter (ein vergifteter Batch soll den
    Gesamt-Import nicht killen)."""
    if not batch:
        return 0
    try:
        sb.table('tail_hex').upsert(batch, on_conflict='registration').execute()
        return len(batch)
    except Exception as e:
        print(f"  [warn] Batch-Upsert fehlgeschlagen ({type(e).__name__}: "
              f"{str(e)[:160]}) — überspringe {len(batch)} Rows.", flush=True)
        return 0


def main():
    sb = _make_supabase_client()
    print(f"Lade OpenSky-Aircraft-Database (streaming):\n  {AIRCRAFT_DB_URL}", flush=True)

    req = urllib.request.Request(AIRCRAFT_DB_URL, headers={
        'User-Agent': USER_AGENT,
        'Accept': 'text/csv',
    })
    try:
        resp = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            sys.exit(f"FEHLER: CSV 404 ({AIRCRAFT_DB_URL}).\n"
                     "Die OpenSky-Dataset-URL hat sich evtl. geändert — siehe Docstring "
                     "(AIRCRAFT_DB_URL=… setzen) bzw. "
                     "https://opensky-network.org/datasets/metadata/")
        sys.exit(f"FEHLER: HTTP {e.code} beim CSV-Download.")
    except urllib.error.URLError as e:
        sys.exit(f"FEHLER: Netzwerk beim CSV-Download: {e.reason}")

    # Streaming: TextIOWrapper über den rohen Response — csv.reader zieht
    # zeilenweise nach, die Datei landet NIE komplett im RAM.
    text_stream = io.TextIOWrapper(resp, encoding='utf-8', errors='replace', newline='')
    reader = csv.reader(text_stream)

    try:
        header = next(reader)
    except StopIteration:
        sys.exit("FEHLER: CSV ist leer.")

    # Spalten-Indizes per Name auflösen (robust gegen Spalten-Umordnung).
    cols = {name.strip().lower(): i for i, name in enumerate(header)}
    needed = ('icao24', 'registration')
    for n in needed:
        if n not in cols:
            sys.exit(f"FEHLER: Pflichtspalte '{n}' fehlt im CSV-Header: {header[:6]}…")
    i_icao = cols['icao24']
    i_reg = cols['registration']
    i_type = cols.get('typecode')
    i_op = cols.get('operator')

    total_seen = 0
    total_skipped = 0
    total_written = 0
    batch = []
    seen_regs = set()  # Dedup innerhalb eines Laufs (CSV kann Reg doppelt führen)

    for raw in reader:
        total_seen += 1
        if i_icao >= len(raw) or i_reg >= len(raw):
            total_skipped += 1
            continue
        icao24 = _clean(raw[i_icao])
        reg = _clean(raw[i_reg])
        # Pflichtfelder: ohne Reg (PK) oder Hex (Lookup-Ziel) ist die Row wertlos.
        if not reg or not icao24:
            total_skipped += 1
            continue
        reg = reg.upper()
        icao24 = icao24.lower()
        if reg in seen_regs:
            # Erste Sichtung gewinnt — der Upsert würde sonst innerhalb DESSELBEN
            # Batches mit identischem PK kollidieren (Supabase lehnt das ab).
            continue
        seen_regs.add(reg)

        row = {
            'registration': reg,
            'icao24': icao24,
            'type_code': _clean(raw[i_type]) if i_type is not None and i_type < len(raw) else None,
            'operator': _clean(raw[i_op]) if i_op is not None and i_op < len(raw) else None,
        }
        batch.append(row)

        if len(batch) >= BATCH_SIZE:
            total_written += _flush(sb, batch)
            batch = []
            if total_written and total_written % (BATCH_SIZE * 20) == 0:
                print(f"  … {total_written} Rows geschrieben "
                      f"({total_seen} gelesen, {total_skipped} übersprungen)", flush=True)

        if ROW_LIMIT and len(seen_regs) >= ROW_LIMIT:
            print(f"  AIRCRAFT_DB_LIMIT={ROW_LIMIT} erreicht — stoppe früh.", flush=True)
            break

    total_written += _flush(sb, batch)
    try:
        text_stream.close()
    except Exception:
        pass

    print("\nFertig.", flush=True)
    print(f"  Gelesen:        {total_seen}", flush=True)
    print(f"  Übersprungen:   {total_skipped} (leere Reg/Hex)", flush=True)
    print(f"  Upserted:       {total_written}", flush=True)
    print("\nMonatlich erneut laufen lassen (manuell oder Cron), um die Stammdaten "
          "frisch zu halten — der Lauf ist idempotent.", flush=True)


if __name__ == '__main__':
    main()
