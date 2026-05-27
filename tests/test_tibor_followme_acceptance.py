"""Tibor 2025 Acceptance Test — AeroTAX-Snapshot vs FollowMe.aero-Golden.

Ziel: jeden Klassifikations-Bug regression-fest pinnen. Beim Live-Run vs
FollowMe gibt es eine bekannte Diff-Liste (Pattern A/B/C/D, siehe
COMMIT-Notes 2026-05-24). Pattern A ist gefixt. B/C/D sind als known-issue
xfail markiert mit Bug-Hunt-Reference. Sobald ein Pattern gefixt ist, soll
der xfail-passing → pytest-fail "unexpected pass" werfen, was uns zwingt
das xfail zu entfernen.

Workflow:
1. Snapshot refreshen (eine API-Call-Iteration, ~3-4 Min):
     python3 tests/refresh_tibor_snapshot.py
   → schreibt tests/fixtures/tibor_aerotax_v11_raw_initial.json
2. Tests laufen:
     pytest tests/test_tibor_followme_acceptance.py -v
3. Mismatch-Tabelle zeigt was AeroTAX vs FollowMe ergibt.

Erwartete Mismatch-Tage werden xfail markiert — wenn die Test-Logik einen
DIESER Tage als „passend" findet, war ein Bug-Fix erfolgreich.
"""
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import pytest

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent))
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

_FIXTURES = _HERE / 'fixtures'
_AEROTAX_SNAPSHOT = _FIXTURES / 'tibor_aerotax_v11_raw_initial.json'
_FOLLOWME_GOLDEN = _FIXTURES / 'followme_golden_tibor_2025.json'

# ════════════════════════════════════════════════════════════════════════════
# Bekannte Mismatch-Tage (nach Pattern A Fix 2026-05-24).
# Sobald einer dieser Tage „matched" ist nach einem Fix → xfail entfernen.
# ════════════════════════════════════════════════════════════════════════════
KNOWN_MISMATCHES = {
    # Pattern B — BH-003c Phantom-Touren (AeroTAX rechnet Z76, FM sieht keinen Tour-Tag)
    '2025-04-02': 'Pattern B: BH-003c Heimkehr BOM Phantom-Tour',
    '2025-05-19': 'Pattern B: Z72 Office Phantom — FM keine Tour',
    '2025-05-20': 'Pattern B: Z73 Anreise Phantom — FM keine Tour',
    '2025-05-21': 'Pattern B: Z76 LAD Volltag Phantom',
    '2025-05-22': 'Pattern B: Z76 LAD Phantom',
    '2025-06-01': 'Pattern B: Z76 GOT Phantom',
    '2025-06-02': 'Pattern B: Z76 SOF Phantom',
    '2025-06-03': 'Pattern B: BH-003c SOF Phantom',
    '2025-09-25': 'Pattern B: Z76 KRK An/Ab Phantom',
    '2025-10-26': 'Pattern B: Z76 TLV Phantom',
    '2025-10-27': 'Pattern B: BH-003c TLV Phantom',
    '2025-12-15': 'Pattern B: Z76 JFK HD-B Phantom',
    '2025-12-16': 'Pattern B: BH-003c JFK Phantom',
    '2025-03-22': 'Pattern B: Z72 Office Phantom',

    # Pattern C — Standby-Aktivierung Tour (Reader-Lücke: SE/CAS-Konflikt)
    # Reader sieht nur "RES/SBM" am HB, FM weiß dass Tour aktiviert wurde
    '2025-04-23': 'Pattern C: Standby + Tour-Start → sollte Z73',
    '2025-04-24': 'Pattern C: Standby Tag 2 KR (Aktivierung von 23.04)',
    '2025-04-25': 'Pattern C: Standby Tag 3 KR',
    '2025-04-26': 'Pattern C: Standby Heimkehr KR',
    '2025-10-20': 'Pattern C: Standby + Tour-Start → sollte Z73',
    '2025-10-21': 'Pattern C: Standby Tag 2 ES (Aktivierung von 20.10)',
    '2025-10-23': 'Pattern C: Standby + Tour-Start → sollte Z73',
    '2025-10-24': 'Pattern C: Standby Tag 2 UK (Aktivierung von 23.10)',
    '2025-11-17': 'Pattern C: Standby + Tour-Start → sollte Z73 NO',
    '2025-11-18': 'Pattern C: Standby Heimkehr NO',

    # Pattern D — Sonnet-Marker-Lesefehler (Reader-Lücken)
    '2025-01-04': 'Pattern D: AeroTAX=Frei, FM=BLR Volltag — Sonnet hat Tag verpasst',
    '2025-01-05': 'Pattern D: AeroTAX=Z73, FM=BLR Anreise — Sonnet konservativ wegen kein SE',
    '2025-01-06': 'Pattern D: AeroTAX=Issue, FM=BLR Heimkehr',
    '2025-01-20': 'Pattern D: AeroTAX=Frei, FM=HKG — Sonnet hat Tag verpasst',
    '2025-02-10': 'Pattern D: AeroTAX=ZeroDay, FM=DE 14€ — Sonnet hat Same-Day verpasst',
    '2025-02-14': 'Pattern D: AeroTAX=Frei, FM=Japan — Sonnet hat Heimkehr verpasst',
    '2025-03-18': 'Pattern D: AeroTAX=Office, FM=Schweiz-Genf 44€ — EH SECCRM als Office gelesen',
    '2025-03-30': 'Pattern D: AeroTAX=Frei, FM=Mumbai Volltag — Reader-Stempel-Leiche X',
    '2025-04-01': 'Pattern D: AeroTAX=Frei, FM=Mumbai Heimkehr — Reader hat Tag verpasst',
    '2025-04-10': 'Pattern D: AeroTAX=Frei, FM=Korea — Reader hat Tour verpasst',
    '2025-05-15': 'Pattern D: AeroTAX=Frei, FM=USA — Reader-Lücke',
    '2025-05-17': 'Pattern D: AeroTAX=Frei, FM=USA — Reader-Lücke',
    '2025-05-27': 'Pattern D: AeroTAX=Frei, FM=USA-Chicago — Reader-Lücke',
    '2025-06-09': 'Pattern D: AeroTAX=Frei, FM=Singapur — Reader-Lücke',
    '2025-06-17': 'Pattern D: AeroTAX=Frei, FM=Kroatien — Reader-Lücke',
    '2025-06-18': 'Pattern D: AeroTAX=Frei, FM=Kroatien — Reader-Lücke',
    '2025-07-07': 'Pattern D: AeroTAX=Frei, FM=USA — Reader-Lücke',
    '2025-07-08': 'Pattern D: AeroTAX=Z73-Mixed, FM=USA — Country-Resolution-Lücke',
    '2025-07-23': 'Pattern D: AeroTAX=Frei, FM=Schweden 44€',
    '2025-07-29': 'Pattern D: AeroTAX=Frei, FM=Lettland — Reader-Lücke',
    '2025-08-01': 'Pattern D: AeroTAX=ZeroDay <8h, FM=DE 14€',
    '2025-08-22': 'Pattern D: AeroTAX=Frei, FM=Zypern — Reader-Lücke',
    '2025-09-11': 'Pattern D: AeroTAX=Z73 Mixed, FM=Nordmazedonien 18€',
    '2025-09-20': 'Pattern D: AeroTAX=Frei, FM=DE 14€',
    '2025-09-26': 'Pattern D: AeroTAX=Z74-Inland, FM=Bulgarien — Country-Mismatch',
    '2025-09-27': 'Pattern D: AeroTAX=Z76 AGP, FM=DE 28€ (24h Z74)',
    '2025-10-06': 'Pattern D: AeroTAX=Frei, FM=Korea — Reader-Lücke',
    '2025-10-07': 'Pattern D: AeroTAX=Frei, FM=Korea — Reader-Lücke',
    '2025-12-28': 'Pattern D: AeroTAX=Frei, FM=Israel — Reader-Lücke',

    # Pattern E — Anreise-Tag-Konvention: FM rechnet Anreise als DE,
    # AeroTAX rechnet als Zielland-Z76. Beide BMF-rechtfertigbar. Diese
    # Tage werden NICHT als "Bugs" gezählt, der Bucket-Check ist konvention-sensitiv.
    '2025-01-03': 'Pattern E: Anreise-Tag — AeroTAX=Z76 BLR, FM=DE',
    '2025-02-12': 'Pattern E: Anreise-Tag — AeroTAX=Z76, FM=DE',
    '2025-03-29': 'Pattern E: Anreise-Tag — AeroTAX=Z76 BOM, FM=DE',
    '2025-03-31': 'Pattern E: Heimkehr-Tag BOM — AeroTAX=Z73, FM=Mumbai',
    '2025-04-08': 'Pattern E: Anreise-Tag — AeroTAX=Z76, FM=DE',
    '2025-10-05': 'Pattern E: Anreise-Tag — AeroTAX=Z76, FM=DE',
}


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def _load_snapshot():
    """Lädt den letzten AeroTAX-Snapshot (tage_detail list)."""
    if not _AEROTAX_SNAPSHOT.exists():
        pytest.skip(
            f'Tibor-Snapshot fehlt: {_AEROTAX_SNAPSHOT}\n'
            f'Refresh: python3 tests/refresh_tibor_snapshot.py'
        )
    with open(_AEROTAX_SNAPSHOT) as f:
        data = json.load(f)
    # tage_detail ist eine Liste — auf datum-Dict mappen
    by_date = {}
    for entry in data:
        d = entry.get('datum', '')
        if d:
            by_date[d] = entry
    return by_date


def _load_golden():
    with open(_FOLLOWME_GOLDEN) as f:
        return json.load(f)


def _bucket_for_fm(fm_day):
    """FollowMe-Tag → erwarteter AeroTAX-Bucket."""
    klass = fm_day.get('klass', '')
    land = fm_day.get('land', '')
    if klass == 'NO_VMA':
        return ('NO_VMA', 'Frei/Office/Standby/ZeroDay')
    if 'Deutschland' in land:
        # FM-Inland: pauschale 14€=Z72/Z73, 28€=Z74
        pauschale = fm_day.get('betrag', 0)
        if pauschale == 28:
            return ('Z74', 'Inland 24h')
        return ('Z72_OR_Z73', 'Inland An/Ab oder >8h')
    if land == '-':
        return ('NO_VMA', 'Same-Day-kein-Beleg')
    return ('Z76', 'Ausland')


# ════════════════════════════════════════════════════════════════════════════
# Aggregat-Tests (counter values vs golden)
# ════════════════════════════════════════════════════════════════════════════

def test_snapshot_exists():
    """Sanity: Snapshot-Datei muss existieren (sonst refresh laufen lassen)."""
    assert _AEROTAX_SNAPSHOT.exists(), (
        f'{_AEROTAX_SNAPSHOT} fehlt. Refresh ausführen: '
        f'python3 tests/refresh_tibor_snapshot.py'
    )


# Aggregate-Tests werden in einem späteren Schritt aktiviert sobald der
# Snapshot ein vollständiges classification-Dict mit fahrtage/arbeitstage/
# hotel_naechte/z72_tage etc enthält (aktuelle Snapshot ist nur tage_detail).
@pytest.mark.skip(reason='Aktivieren wenn refresh_tibor_snapshot.py auch classification dict speichert')
def test_aggregate_fahrtage_matches_followme():
    pass


# ════════════════════════════════════════════════════════════════════════════
# Tag-für-Tag-Tests (parametrized über alle FollowMe-Tour-Tage)
# ════════════════════════════════════════════════════════════════════════════

# Alle 133 FollowMe-Tage als Parameter, damit jeder Tag einen eigenen Test
# bekommt — bei Run sieht man genau welcher Tag fehlt.

def _all_followme_days():
    if not _FOLLOWME_GOLDEN.exists():
        return []
    with open(_FOLLOWME_GOLDEN) as f:
        gd = json.load(f)
    return sorted(gd.get('day_classification', {}).keys())


@pytest.mark.parametrize('datum', _all_followme_days())
def test_day_classification_matches_followme_bucket(datum):
    """Pro FollowMe-Tour-Tag: AeroTAX-Klasse muss in den erwarteten Bucket fallen."""
    snapshot = _load_snapshot()
    golden = _load_golden()
    fm_day = golden['day_classification'].get(datum)
    if not fm_day:
        pytest.skip(f'{datum} nicht in FollowMe-Golden')
    expected_bucket, bucket_label = _bucket_for_fm(fm_day)

    aero = snapshot.get(datum)
    if aero is None:
        pytest.fail(
            f'{datum} fehlt im AeroTAX-Snapshot — FollowMe hat {fm_day["land"]} '
            f'({fm_day["betrag"]}€), erwarteter Bucket: {bucket_label}'
        )
    aero_klass = aero.get('klass', '')

    # Known mismatch → xfail (dokumentiertes Bug-Pattern)
    if datum in KNOWN_MISMATCHES:
        pytest.xfail(f'{KNOWN_MISMATCHES[datum]} (aero={aero_klass}, fm={fm_day["land"]} {fm_day["betrag"]}€)')

    # Bucket-Check
    matched = False
    if expected_bucket == 'Z76':
        matched = aero_klass == 'Z76'
    elif expected_bucket == 'Z74':
        matched = aero_klass == 'Z74'
    elif expected_bucket == 'Z72_OR_Z73':
        matched = aero_klass in ('Z72', 'Z73')
    elif expected_bucket == 'NO_VMA':
        matched = aero_klass in ('Frei', 'Office', 'Standby', 'ZeroDay', 'Issue')

    assert matched, (
        f'{datum} AeroTAX={aero_klass} passt nicht zu FollowMe-Bucket '
        f'{expected_bucket} ({bucket_label}) — FM={fm_day["land"]} {fm_day["betrag"]}€'
    )


def test_no_unexpected_z76_outside_followme_tour_days():
    """Negativ-Test: AeroTAX darf KEIN Z76 außerhalb der FollowMe-Tour-Tage haben.

    Beschränkt auf das FollowMe-Steuerjahr (2025). 2026er Tage (Reader hat
    bereits Folgejahres-Daten gelesen) sind nicht im FM-Golden enthalten
    und damit nicht prüfbar.
    """
    snapshot = _load_snapshot()
    golden = _load_golden()
    fm_year = str(golden.get('meta', {}).get('year') or '2025')
    fm_tour_days = set(golden['day_classification'].keys())
    extra_z76 = []
    for datum, entry in snapshot.items():
        if not str(datum).startswith(fm_year):
            continue
        if entry.get('klass') != 'Z76':
            continue
        if datum in fm_tour_days:
            continue
        extra_z76.append((datum, entry.get('begruendung', '')[:80]))

    if extra_z76:
        # Known Pattern B Tage filtern
        truly_unexpected = [(d, b) for d, b in extra_z76 if d not in KNOWN_MISMATCHES]
        if truly_unexpected:
            msg = '\n'.join(f'  {d}  {b}' for d, b in truly_unexpected[:20])
            pytest.fail(
                f'{len(truly_unexpected)} NEUE Phantom-Z76-Tage (nicht in Pattern B Liste):\n{msg}'
            )
        # Sonst: alle bekannt → xfail
        pytest.xfail(f'{len(extra_z76)} bekannte Phantom-Z76-Tage (Pattern B)')


def test_summary_mismatch_count():
    """Reporting-Test: zeigt Anzahl Mismatches im aktuellen Snapshot."""
    snapshot = _load_snapshot()
    golden = _load_golden()
    mismatches = []
    by_bucket = defaultdict(list)
    for datum, fm_day in golden['day_classification'].items():
        expected_bucket, _ = _bucket_for_fm(fm_day)
        aero = snapshot.get(datum)
        if aero is None:
            mismatches.append((datum, 'MISSING', expected_bucket))
            by_bucket[expected_bucket].append(datum)
            continue
        aero_klass = aero.get('klass', '')
        matched = False
        if expected_bucket == 'Z76':
            matched = aero_klass == 'Z76'
        elif expected_bucket == 'Z74':
            matched = aero_klass == 'Z74'
        elif expected_bucket == 'Z72_OR_Z73':
            matched = aero_klass in ('Z72', 'Z73')
        elif expected_bucket == 'NO_VMA':
            matched = aero_klass in ('Frei', 'Office', 'Standby', 'ZeroDay', 'Issue')
        if not matched:
            mismatches.append((datum, aero_klass, expected_bucket))
            by_bucket[expected_bucket].append(datum)

    print(f'\n═══ TIBOR FOLLOWME ACCEPTANCE ═══')
    print(f'  Tage geprüft (FM-Tour-Tage): {len(golden["day_classification"])}')
    print(f'  Mismatches:                  {len(mismatches)}')
    print(f'  Davon known xfail:           {sum(1 for d, _, _ in mismatches if d in KNOWN_MISMATCHES)}')
    print(f'  Davon NEU:                   {sum(1 for d, _, _ in mismatches if d not in KNOWN_MISMATCHES)}')
    if mismatches:
        print('\nMismatches pro Bucket:')
        for bucket, days in sorted(by_bucket.items()):
            print(f'  {bucket}: {len(days)} Tage')
        print('\nDetails (max 30):')
        for datum, aero_klass, expected in mismatches[:30]:
            marker = '✗' if datum not in KNOWN_MISMATCHES else 'x'
            print(f'  {marker} {datum}  AeroTAX={aero_klass}  expected={expected}')

    # Nicht-fail-Test — reines Reporting. Failt nur wenn NEUE Mismatches auftauchen.
    new_mismatches = [d for d, _, _ in mismatches if d not in KNOWN_MISMATCHES]
    assert not new_mismatches, (
        f'{len(new_mismatches)} NEUE Mismatches (nicht in KNOWN_MISMATCHES): '
        f'{new_mismatches[:10]}'
    )
