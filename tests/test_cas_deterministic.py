"""Tests fuer den deterministischen CAS-Parser + Reconcile-Layer."""
import os
import sys
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent))
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import pytest  # noqa: E402
import cas_table_parser as ctp  # noqa: E402
import cas_reconcile as rec  # noqa: E402
import conftest as _cft  # noqa: E402

REAL_PDF = _cft.private_doc('Steuer 25', 'CAS', 'PUB_1_1_0_1220115246_2024-12-20.pdf')
_HAVE_PDF = os.path.exists(REAL_PDF)


# ── Reconcile-Logik (ohne PDF, reine Funktion) ─────────────────────────────

def test_reconcile_overrides_flightnumbers_deterministically():
    det = {'datum': '2025-01-14', 'flight_numbers': ['LH418'], 'routing': ['FRA', 'IAD'],
           'dep_time': '11:55', 'arr_time': '20:50'}
    llm = {'datum': '2025-01-14', 'flight_numbers': [], 'has_flight_segment': False,
           'routing_iatas': [], 'overnight_after_day': False}
    out = rec.reconcile_day(det, llm, 'FRA')
    assert out['flight_numbers'] == ['LH418']
    assert out['has_flight_segment'] is True
    assert 'IAD' in out['routing_iatas']
    assert out.get('reconcile')


def test_reconcile_detects_night_return_over_midnight():
    # LH419 IAD dep 22:50 UTC 15.01 -> FRA arr 07:35 UTC 16.01 (UTC-Mitternacht ueberquert)
    det = {'datum': '2025-01-15', 'flight_numbers': ['LH419'], 'routing': ['IAD', 'FRA'],
           'dep_time': '22:50', 'arr_time': '07:35'}
    llm = {'datum': '2025-01-15', 'overnight_after_day': False}
    out = rec.reconcile_day(det, llm, 'FRA')
    assert out['overnight_after_day'] is True, out.get('reconcile')


def test_reconcile_no_change_when_already_correct():
    # LH418 FRA->IAD landet im Ausland (Washington), Crew bleibt dort über Nacht
    # → overnight_vma=True ist KORREKT. Wenn der LLM das schon richtig hatte,
    # darf es keine Korrektur geben.
    det = {'datum': '2025-01-14', 'flight_numbers': ['LH418'], 'routing': ['FRA', 'IAD'],
           'dep_time': '11:55', 'arr_time': '20:50'}
    llm = {'datum': '2025-01-14', 'flight_numbers': ['LH418'], 'has_flight_segment': True,
           'routing_iatas': ['FRA', 'IAD'], 'overnight_after_day': True}
    out = rec.reconcile_day(det, llm, 'FRA')
    assert not out.get('reconcile'), out.get('reconcile')
    # tz-Flags trotzdem gesetzt (Audit-Transparenz), auch ohne Korrektur
    assert out.get('tz_overnight_vma') is True
    assert out.get('tz_hotel_night') is True


def test_reconcile_flips_wrong_overnight_for_foreign_layover():
    # LLM sagt fälschlich overnight=False für einen Flug ins Ausland → wird korrigiert.
    det = {'datum': '2025-01-14', 'flight_numbers': ['LH418'], 'routing': ['FRA', 'IAD'],
           'dep_time': '11:55', 'arr_time': '20:50'}
    llm = {'datum': '2025-01-14', 'flight_numbers': ['LH418'], 'has_flight_segment': True,
           'routing_iatas': ['FRA', 'IAD'], 'overnight_after_day': False}
    out = rec.reconcile_day(det, llm, 'FRA')
    assert out['overnight_after_day'] is True
    assert out.get('reconcile')


def test_reconcile_days_reports_det_only_dates():
    det_days = [{'datum': '2025-01-14', 'flight_numbers': ['LH418'], 'routing': ['FRA', 'IAD'],
                 'dep_time': '11:55', 'arr_time': '20:50'},
                {'datum': '2025-01-16', 'flight_numbers': [], 'routing': ['FRA'],
                 'dep_time': '07:35', 'arr_time': '07:35'}]
    llm_days = [{'datum': '2025-01-14', 'flight_numbers': []}]
    r = rec.reconcile_days(det_days, llm_days, 'FRA')
    assert '2025-01-16' in r['det_only_dates']
    assert r['corrections_count'] >= 1


# ── Echter PDF-Parse (deterministisch reproduzierbar) ──────────────────────

@pytest.mark.skipif(not _HAVE_PDF, reason='Echte CAS-PDF nicht vorhanden')
def test_real_pdf_parses_year_and_homebase():
    r = ctp.parse_cas_pdf(REAL_PDF)
    assert r['year'] == 2025
    assert r['homebase'] == 'FRA'


@pytest.mark.skipif(not _HAVE_PDF, reason='Echte CAS-PDF nicht vorhanden')
def test_real_pdf_extracts_known_flights():
    r = ctp.parse_cas_pdf(REAL_PDF)
    all_fl = {fn for d in r['days'] for fn in d['flight_numbers']}
    # Diese Fluege stehen nachweislich im PDF
    for fn in ('LH418', 'LH419', 'LH716', 'LH717'):
        assert fn in all_fl, f'{fn} fehlt — gefunden: {sorted(all_fl)}'


@pytest.mark.skipif(not _HAVE_PDF, reason='Echte CAS-PDF nicht vorhanden')
def test_real_pdf_flight_times_are_correct():
    # Die exakten UTC-Zeiten aus dem PDF (Fix gegen Block/Briefing-Spalten-Verwechslung)
    r = ctp.parse_cas_pdf(REAL_PDF)
    by_fl = {}
    for d in r['days']:
        for fn in d['flight_numbers']:
            by_fl.setdefault(fn, (d['dep_time'], d['arr_time']))
    assert by_fl['LH418'] == ('11:55', '20:50'), by_fl.get('LH418')
    assert by_fl['LH717'] == ('03:35', '18:00'), by_fl.get('LH717')
    # LH716 ist ein Langstrecken-Nachtflug: dep steht in der Tag-Zeile, die
    # Ankunft physisch in der Folgezeile → arr hier None ist KORREKT (nicht geraten).
    assert by_fl['LH716'][0] == '13:05', by_fl.get('LH716')


@pytest.mark.skipif(not _HAVE_PDF, reason='Echte CAS-PDF nicht vorhanden')
def test_real_pdf_all_days_dated_and_no_junk_routing():
    r = ctp.parse_cas_pdf(REAL_PDF)
    # 100% datiert (Monatszuordnung korrekt, kein Vormonats-Rollover-Bug)
    assert all(d['datum'] for d in r['days']), \
        f"undatiert: {[(d['weekday'], d['day_of_month']) for d in r['days'] if not d['datum']]}"
    # PUB_1 ist der Januar-Plan → Hauptmonat 2025-01
    jan_days = [d for d in r['days'] if d['datum'] and d['datum'].startswith('2025-01')]
    assert len(jan_days) >= 28, f'zu wenige Januar-Tage: {len(jan_days)}'
    # kein Monatskürzel/Header-Müll im Routing
    junk = {'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP',
            'OCT', 'NOV', 'DEC', 'LIN', 'YA'}
    found = {t for d in r['days'] for t in d['routing'] if t in junk}
    assert not found, f'Routing-Müll: {found}'


@pytest.mark.skipif(not _HAVE_PDF, reason='Echte CAS-PDF nicht vorhanden')
def test_real_pdf_flight_day_dates_correct():
    # Kernbeweis gegen den Monats-Verschiebungs-Bug: Flüge im JANUAR, nicht Februar.
    r = ctp.parse_cas_pdf(REAL_PDF)
    by_fl = {fn: d['datum'] for d in r['days'] for fn in d['flight_numbers']}
    assert by_fl['LH418'] == '2025-01-14', by_fl.get('LH418')
    assert by_fl['LH717'] == '2025-01-23', by_fl.get('LH717')


def test_foreign_layout_returns_confidence_none():
    # Defensiv: PDF-Bytes, die NICHT nach CAS aussehen → Parser tritt zurück.
    # (Wir simulieren über layout_ok mit Fake-Rows.)
    rows = [(0, 'Irgendein anderes Dokument'), (0, 'Rechnung Nr 12345'),
            (0, 'Betrag 42,00 EUR')]
    assert ctp.layout_ok(rows) is False


def test_cas_layout_detected_as_ok():
    rows = [(0, 'Crew Assignment System v18'), (0, 'Mo 14 49264 FB'),
            (0, 'Briefingzeit(LT FRA): 14/01/25 11:05'), (0, 'Alle zeiten in UTC')]
    assert ctp.layout_ok(rows) is True


@pytest.mark.skipif(not _HAVE_PDF, reason='Echte CAS-PDF nicht vorhanden')
def test_real_pdf_overnight_arrivals_backfilled():
    # Nachtflug-Ankunft aus der Folgezeile: LH419/LH716 bekommen arr_time gesetzt.
    r = ctp.parse_cas_pdf(REAL_PDF)
    by = {fn: (d['dep_time'], d['arr_time']) for d in r['days'] for fn in d['flight_numbers']}
    assert by['LH419'] == ('22:50', '07:35'), by.get('LH419')
    assert by['LH716'] == ('13:05', '12:40'), by.get('LH716')


@pytest.mark.skipif(not _HAVE_PDF, reason='Echte CAS-PDF nicht vorhanden')
def test_real_pdf_vma_vs_hotel_flags_are_tax_correct():
    # Die steuerlich getrennten Flags an echten Fluegen (BMF §9 EStG):
    #  - Hinflug zum Layover am selben Tag (LH418 FRA->IAD): vma+hotel
    #  - Hinflug ueber Nacht (LH716 FRA->HND): vma ja, hotel nein (im Flug)
    #  - Heimflug ueber Nacht (LH419 IAD->FRA): vma ja (Reisetag), hotel nein
    #  - Heimflug am selben Tag (LH717 HND->FRA): vma nein, hotel nein
    r = ctp.parse_cas_pdf(REAL_PDF)
    det = {fn: d for d in r['days'] for fn in d['flight_numbers']}
    cases = {
        'LH418': (True, True), 'LH716': (True, False),
        'LH419': (True, False), 'LH717': (False, False),
    }
    for fn, (exp_vma, exp_hotel) in cases.items():
        vh = rec.compute_vma_and_hotel(det[fn], 'FRA')
        assert vh.get('overnight_vma') is exp_vma, f'{fn} vma {vh}'
        assert vh.get('hotel_night') is exp_hotel, f'{fn} hotel {vh}'


def test_vma_hotel_no_arrival_time_is_conservative():
    # Auswaerts-Flug ohne Ankunftszeit: vma sicher True, hotel unbestimmt (None).
    det = {'datum': '2025-01-19', 'flight_numbers': ['LH716'],
           'routing': ['FRA', 'HND'], 'dep_time': '13:05', 'arr_time': None}
    vh = rec.compute_vma_and_hotel(det, 'FRA')
    assert vh['overnight_vma'] is True
    assert vh['hotel_night'] is None


def test_vma_hotel_same_day_return_home_no_vma():
    det = {'datum': '2025-01-23', 'flight_numbers': ['LH717'],
           'routing': ['HND', 'FRA'], 'dep_time': '03:35', 'arr_time': '18:00'}
    vh = rec.compute_vma_and_hotel(det, 'FRA')
    assert vh['overnight_vma'] is False
    assert vh['hotel_night'] is False


@pytest.mark.skipif(not _HAVE_PDF, reason='Echte CAS-PDF nicht vorhanden')
def test_real_pdf_is_deterministic_across_runs():
    # Kernversprechen: gleiche Eingabe -> exakt gleiche harte Fakten
    r1 = ctp.parse_cas_pdf(REAL_PDF)
    r2 = ctp.parse_cas_pdf(REAL_PDF)
    f1 = [(d['datum'], tuple(d['flight_numbers']), d['dep_time'], d['arr_time']) for d in r1['days']]
    f2 = [(d['datum'], tuple(d['flight_numbers']), d['dep_time'], d['arr_time']) for d in r2['days']]
    assert f1 == f2
