"""Monatsplan-PDF `_briefing_to_klass_routing`: Same-Day-Return-Guard.

WURZEL (Audit 2026-07-18): der Label-Ableiter druckte JEDEN Flugtag mit Legs (oder
einem „-" im Summary) als Z76 (Ausland-Layover-Farbe) — OHNE Homebase-Rückkehr-
Check. Ein Same-Day-Turnaround FRA-LUX-FRA (endet an der Homebase) bekam damit die
Layover-Farbe statt der Tour-Farbe.

Hier festgenagelt: endet der Tag mit seiner letzten Ankunft an der Homebase, ist es
Z72 (Tour), nicht Z76. Ein echter Outstation-/Layover-Tag (FRA-JFK) bleibt Z76.
Reine LAYOVER-Marker-Tage bleiben immer Z76 (autoritativ).

Nur Label/Farbe — keine Steuerklassifikation. Getestet über den echten PDF-Endpoint
(die Funktion ist nested); der Tages-Zellen-Text zeigt das Label.
"""
import os
import sys
import io
import datetime as _dt

import pytest

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import app as A

pdfplumber = pytest.importorskip('pdfplumber')


def _day_cell_lines(briefs, hb='FRA'):
    """Rendert den Monatsplan und liefert die Textzeilen zwischen den Kalender-
    Zahlenzeilen (die Label/Routing-Zeilen der belegten Tage)."""
    tok = 'pdf_turnaround_tok'
    d = list(briefs.keys())[0]
    month = d[:7]
    A._store[tok] = {'result_data': {}}
    # Stubs NUR für diesen Render — Originale IMMER restaurieren (Suite-Leak
    # 2026-07-20: die global ersetzten Loader vergifteten jeden späteren Test,
    # der echte Profil-/Briefing-Persistenz liest — test_crewaccess_pdf war
    # Suite-rot, solo grün).
    _orig = (A._manual_briefings_load, A._ical_briefings_load, A._profile_load)
    A._manual_briefings_load = lambda t: {}
    A._ical_briefings_load = lambda t: briefs
    A._profile_load = lambda t: {'profile': {'homebase': hb}}
    try:
        client = A.app.test_client()
        r = client.get(f'/api/user/calendar-pdf/{tok}?month={month}')
        assert r.status_code == 200
        assert r.data[:4] == b'%PDF'
    finally:
        A._manual_briefings_load, A._ical_briefings_load, A._profile_load = _orig
    with pdfplumber.open(io.BytesIO(r.data)) as pdf:
        text = pdf.pages[0].extract_text() or ''
    # Footer-Legende („… · Z76 Ausland · …") NICHT mitzählen.
    body = text.split('Generiert mit AeroTax')[0]
    return body


def _mid_month_day():
    return _dt.date.today().replace(day=15).isoformat()


def test_same_day_turnaround_to_homebase_is_tour_z72():
    """FRA-LUX-FRA endet an der Homebase → Z72 (Tour), NICHT Z76."""
    d = _mid_month_day()
    briefs = {d: {'ical_summary': 'LH1234 FRA-LUX',
                  'legs': [{'from': 'FRA', 'to': 'LUX'},
                           {'from': 'LUX', 'to': 'FRA'}]}}
    body = _day_cell_lines(briefs, hb='FRA')
    assert 'Z72' in body
    assert 'Z76' not in body   # Footer wurde abgeschnitten


def test_outstation_overnight_stays_layover_z76():
    """FRA-JFK endet NICHT an der Homebase → Z76 (Layover) bleibt."""
    d = _mid_month_day()
    briefs = {d: {'ical_summary': 'LH400 FRA-JFK',
                  'legs': [{'from': 'FRA', 'to': 'JFK'}]}}
    body = _day_cell_lines(briefs, hb='FRA')
    assert 'Z76' in body
    assert 'Z72' not in body


def test_explicit_layover_marker_stays_z76():
    """Ein reiner LAYOVER-Marker-Tag bleibt Z76, auch wenn die Homebase im Text
    auftaucht (autoritativer Layover-Marker)."""
    d = _mid_month_day()
    briefs = {d: {'ical_summary': 'LAYOVER (Tag 2/3)',
                  'ical_location': 'BLR'}}
    body = _day_cell_lines(briefs, hb='FRA')
    assert 'Z76' in body
    assert 'Z72' not in body


def test_muc_base_turnaround_not_hardcoded_fra():
    """MUC-Base: MUC-VIE-MUC endet an MUC (Homebase) → Z72. Kein FRA-Hardcode."""
    d = _mid_month_day()
    briefs = {d: {'ical_summary': 'LH1780 MUC-VIE',
                  'legs': [{'from': 'MUC', 'to': 'VIE'},
                           {'from': 'VIE', 'to': 'MUC'}]}}
    body = _day_cell_lines(briefs, hb='MUC')
    assert 'Z72' in body
    assert 'Z76' not in body
