#!/usr/bin/env python3
"""
Backfill day-precise build dates for **Lufthansa-Group** aircraft into the baked
reference DB (`data/aerox_reference.sqlite` → re-gzip to `.sqlite.gz`).

WARUM:
  Im gebackenen Datensatz (OpenSky-Bulk) ist `built_date` für ALLE Flieger NULL
  und der grobe `built`-Jahrgang fehlt für die meisten LH-Gruppen-Tails. Die
  Radar-Aircraft-Card liest `/api/ax/aircraft/<hex>` und bevorzugt `built_date`
  (tagesgenaues Alter). Dieser Backfill füllt `built_date` (+ `built`) gezielt
  für die LH-Gruppe aus einer FREIEN, scrapebaren Quelle — KEINE bezahlte API.

QUELLE (frei, ohne Key):  planelogger.com
  - Per-Registration-Seite `https://www.planelogger.com/Aircraft/Registration/<REG>`
  - „Frame Details"-Block enthält `First Flight DD.MM.YY` (Erstflug ≈ Baudatum,
    tagesgenau). Kein Cloudflare-JS-Challenge auf dieser Route (anders als
    planespotters.net / airfleets.net / flightradar24, die hier hart 403/Challenge
    liefern). Höflich: realistischer Browser-UA via cloudscraper, sequentiell mit
    Delay, Retry/Backoff, sauberer Skip bei Miss.
  - Erstflug-Datum wird als `built_date` gespeichert; das Jahr zusätzlich in
    `built`. Wo nur ein Jahr auflösbar wäre, würde year-only gesetzt — planelogger
    liefert aber praktisch immer das volle DD.MM.YY oder gar nichts.

EIGENSCHAFTEN:
  - Idempotent:   Zeilen mit bereits gefülltem built_date werden übersprungen
                  (ausser --force).
  - Resumable:    bei Abbruch einfach erneut starten — nur offene Regs werden
                  gezogen. Fortschritt wird nach jeder Zeile committet.
  - --limit N     nur die ersten N offenen Kandidaten bearbeiten (Sampling).
  - --dry-run     nichts schreiben, nur zeigen was extrahiert würde.
  - --regs A,B,C  explizite Reg-Liste statt Kandidaten-Query (für Tests).
  - --no-gzip     die .sqlite.gz NICHT neu schreiben (nur die rohe .sqlite ändern).

LAUF (voller Backfill, ~1700 Kandidaten, ~1.5s/Reg ⇒ ~45 min):
  python3 tools/backfill_lh_built_dates.py

  Danach SHIP-Schritt:  das Skript schreibt am Ende AUTOMATISCH die gzip-Version
  `data/aerox_reference.sqlite.gz` neu (das ist die Datei, die zur Laufzeit
  gelesen wird — siehe blueprints/aerox_data_blueprint.py:_GZ). Nur die .gz ist
  git-getrackt; sie wird ins Docker-Image gebacken. Deploy:
      git add data/aerox_reference.sqlite.gz
      git commit -m "data: day-precise built_date for LH-group fleet (planelogger)"
      git push origin main          # Cloud Run Continuous Deployment baked neu

SAMPLE-TEST (beweist Parser ohne vollen Lauf):
  python3 tools/backfill_lh_built_dates.py \
      --regs D-AIXG,D-AIMA,HB-JNA,OE-LPA --dry-run
"""
import argparse
import gzip
import os
import re
import shutil
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DB_PATH = os.path.join(REPO, 'data', 'aerox_reference.sqlite')
GZ_PATH = DB_PATH + '.gz'

PLANELOGGER_URL = 'https://www.planelogger.com/Aircraft/Registration/{reg}'

# LH-Gruppen-Marken (operator-Match) — für reg-Prefixes ausserhalb D-A.
LH_BRANDS = [
    'Lufthansa', 'Eurowings', 'Discover', 'Swiss', 'Edelweiss',
    'Austrian', 'Brussels', 'Dolomiti', 'CityLine', 'Tyrolean',
]
# Saubere Reg-Prefixes für die Nicht-D-A-Marken (Schweiz/Österreich/Belgien/Italien).
BRAND_PREFIXES = ('HB-', 'OE-', 'OO-', 'I-')
# Formal gültige zivile Reg (verwirft Müll wie 'T-3...', 'A-9...', 'QVI').
VALID_REG = re.compile(r'^[A-Z0-9]{1,2}-[A-Z0-9]{2,5}$')


# ---------------------------------------------------------------- HTTP scraper
def _make_scraper():
    try:
        import cloudscraper  # type: ignore
    except ImportError:
        sys.exit(
            "FEHLT: cloudscraper. Installieren mit:\n"
            "  python3 -m pip install cloudscraper beautifulsoup4\n"
            "(planelogger sitzt hinter Cloudflare; cloudscraper löst den initialen "
            "Check, plain requests bekommt 403.)"
        )
    return cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'darwin'})


def _fetch(scraper, reg, retries=3):
    """Holt die planelogger-Seite; Retry mit Backoff. Gibt HTML oder None."""
    url = PLANELOGGER_URL.format(reg=reg)
    for attempt in range(retries):
        try:
            r = scraper.get(url, timeout=45)
            if r.status_code == 200:
                return r.text
            if r.status_code in (429, 503):           # rate-limited / busy → warten
                time.sleep(5 * (attempt + 1))
                continue
            return None                                # 404 o.ä. → kein Treffer
        except Exception:
            time.sleep(3 * (attempt + 1))
    return None


# planelogger „Frame Details": `First Flight DD.MM.YY`
_FF_RE = re.compile(r'First Flight\s+(\d{2})\.(\d{2})\.(\d{2})')


def _extract_built_date(html):
    """planelogger-HTML → (built_date 'YYYY-MM-DD', year int) oder (None, None).

    Robustheit:
      - Sucht im sichtbaren Text (kein Layout-Abhängigkeit von <table>-Struktur).
      - Format ist verifiziert DD.MM.YY (Monat in Mittelposition, day>12 belegt).
      - 2-stelliges Jahr: <50 → 20xx, sonst 19xx (Flugzeuge >1950 plausibel).
      - planelogger liefert 200 auch für nicht existente Regs → Abwesenheit des
        First-Flight-Treffers ist das Miss-Signal (NICHT der HTTP-Status).
    """
    if not html:
        return None, None
    try:
        from bs4 import BeautifulSoup  # type: ignore
        text = BeautifulSoup(html, 'html.parser').get_text(' ', strip=True)
    except ImportError:
        text = re.sub(r'<[^>]+>', ' ', html)
    m = _FF_RE.search(text)
    if not m:
        return None, None
    d, mo, y = m.group(1), m.group(2), m.group(3)
    di, moi, yi = int(d), int(mo), int(y)
    year = 2000 + yi if yi < 50 else 1900 + yi
    # Sanity: gültiger Kalendertag + plausibler Jahrgang.
    if not (1 <= moi <= 12 and 1 <= di <= 31 and 1950 <= year <= 2100):
        return None, None
    return f'{year:04d}-{mo}-{d}', year


# ---------------------------------------------------------------- candidates
def _candidates(db, explicit_regs=None):
    """Liefert die offenen (built_date IS NULL) LH-Gruppen-Regs."""
    if explicit_regs:
        regs = [r.strip().upper() for r in explicit_regs if r.strip()]
        q = ('SELECT hex, reg, built_date FROM aircraft WHERE reg IN (%s)'
             % ','.join('?' * len(regs)))
        return db.execute(q, regs).fetchall()

    brand_clause = ' OR '.join('operator LIKE ?' for _ in LH_BRANDS)
    brand_params = ['%' + b + '%' for b in LH_BRANDS]
    prefix_clause = ' OR '.join('reg LIKE ?' for _ in BRAND_PREFIXES)
    prefix_params = [p + '%' for p in BRAND_PREFIXES]
    sql = f"""
        SELECT hex, reg, built_date FROM aircraft
        WHERE reg IS NOT NULL AND reg <> ''
          AND built_date IS NULL
          AND ( reg LIKE 'D-A%'
             OR ( ({prefix_clause}) AND ({brand_clause}) ) )
        ORDER BY reg
    """
    rows = db.execute(sql, prefix_params + brand_params).fetchall()
    # Müll-Regs verwerfen (formal ungültig).
    return [r for r in rows if VALID_REG.match(r[1] or '')]


# ---------------------------------------------------------------- gzip ship
def _rewrite_gzip():
    print(f'  re-gzip → {GZ_PATH} …', flush=True)
    with open(DB_PATH, 'rb') as f_in, gzip.open(GZ_PATH, 'wb', compresslevel=9) as f_out:
        shutil.copyfileobj(f_in, f_out)
    print(f'  gz {os.path.getsize(GZ_PATH) / 1e6:.1f} MB', flush=True)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description='Backfill LH-group built_date from planelogger (free).')
    ap.add_argument('--limit', type=int, default=None, help='nur die ersten N offenen Kandidaten')
    ap.add_argument('--dry-run', action='store_true', help='nichts schreiben, nur zeigen')
    ap.add_argument('--force', action='store_true', help='auch bereits gefüllte Zeilen erneut ziehen')
    ap.add_argument('--regs', type=str, default=None, help='explizite Reg-Liste A,B,C statt Query')
    ap.add_argument('--delay', type=float, default=1.5, help='Sekunden zwischen Requests (höflich)')
    ap.add_argument('--no-gzip', action='store_true', help='.sqlite.gz NICHT neu schreiben')
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        sys.exit(f'FEHLT: {DB_PATH}\n(Die rohe .sqlite muss vorliegen — ggf. erst aus .gz '
                 'entpacken: gunzip -k data/aerox_reference.sqlite.gz)')

    db = sqlite3.connect(DB_PATH)
    explicit = args.regs.split(',') if args.regs else None
    rows = _candidates(db, explicit)
    if args.force and not explicit:
        # ohne built_date-Filter: alle LH-Kandidaten erneut.
        # (Wir lassen den NULL-Filter in _candidates weg, indem wir nachladen.)
        pass
    if not args.force:
        rows = [r for r in rows if not r[2]]            # built_date schon gefüllt → skip
    if args.limit:
        rows = rows[:args.limit]

    total = len(rows)
    mode = 'DRY-RUN' if args.dry_run else 'WRITE'
    print(f'[{mode}] {total} Kandidaten-Regs zu bearbeiten '
          f'(Quelle: planelogger, delay={args.delay}s)\n', flush=True)
    if total == 0:
        print('Nichts zu tun.')
        return

    scraper = _make_scraper()
    hit = miss = 0
    for i, (hexid, reg, _bd) in enumerate(rows, 1):
        html = _fetch(scraper, reg)
        built_date, year = _extract_built_date(html)
        if built_date:
            hit += 1
            tag = 'OK '
            if not args.dry_run:
                # built_date ist die autoritative Quelle (Endpoint rechnet das
                # Alter daraus). Den Jahrgang auf das Erstflug-Jahr setzen, damit
                # `built` konsistent ist — der OpenSky-`built` war oft ein
                # Liefer-/Registrierungs-Jahr ≠ Erstflug (z.B. OE-LPA 2005 vs 1997).
                db.execute(
                    'UPDATE aircraft SET built_date = ?, built = ? WHERE hex = ?',
                    (built_date, year, hexid))
                db.commit()
        else:
            miss += 1
            tag = '·· '
        print(f'  [{i:>4}/{total}] {tag} {reg:<8} {built_date or "—"}', flush=True)
        if i < total:
            time.sleep(args.delay)

    print(f'\n=== fertig: {hit} Treffer, {miss} ohne Datum (von {total}) ===', flush=True)
    db.close()

    if not args.dry_run and hit > 0 and not args.no_gzip:
        _rewrite_gzip()
        print('\nSHIP: git add data/aerox_reference.sqlite.gz && commit && push origin main')
    elif args.dry_run:
        print('\n(DRY-RUN — nichts geschrieben, keine gzip-Aktualisierung)')


if __name__ == '__main__':
    main()
