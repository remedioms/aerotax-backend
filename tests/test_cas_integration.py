"""Tests fuer cas_integration.reconcile_cas_days (die Live-Pipeline-Bruecke)."""
import os
import sys
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent))
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import pytest  # noqa: E402
import cas_integration as ci  # noqa: E402

REAL_PDF = '/Users/miguelschumann/Desktop/Steuer 25/CAS/PUB_1_1_0_1220115246_2024-12-20.pdf'
_HAVE_PDF = os.path.exists(REAL_PDF)


def test_flag_off_returns_unchanged():
    days = [{'datum': '2025-01-14', 'flight_numbers': []}]
    out, audit = ci.reconcile_cas_days(b'x', days, 'FRA')  # force=False, env off
    assert out is days
    assert audit['applied'] is False
    assert audit['reason'] == 'flag_off'


def test_no_bytes_is_safe():
    days = [{'datum': '2025-01-14'}]
    out, audit = ci.reconcile_cas_days(None, days, 'FRA', force=True)
    assert out is days
    assert audit['reason'] == 'no_cas_bytes'


def test_foreign_bytes_layout_not_recognized():
    # zufaellige Bytes -> Parser wirft oder confidence none -> defensiv unveraendert
    days = [{'datum': '2025-01-14'}]
    out, audit = ci.reconcile_cas_days(b'%PDF-1.4 not really', days, 'FRA', force=True)
    assert out is days
    assert audit['applied'] is False


@pytest.mark.skipif(not _HAVE_PDF, reason='Echte CAS-PDF nicht vorhanden')
def test_real_pdf_reconcile_applies_and_corrects():
    with open(REAL_PDF, 'rb') as f:
        cas_bytes = f.read()
    # Simuliere einen LLM, der bei LH419 (Nachtflug-Heimkehr) overnight FALSCH
    # auf False gesetzt hat und LH418 als Nicht-Flug verkannt hat.
    llm_days = [
        {'datum': '2025-01-14', 'flight_numbers': [], 'has_flight_segment': False,
         'routing_iatas': [], 'overnight_after_day': False},
        {'datum': '2025-01-15', 'flight_numbers': ['LH419'], 'has_flight_segment': True,
         'routing_iatas': ['IAD', 'FRA'], 'overnight_after_day': False},
        {'datum': '2025-01-23', 'flight_numbers': ['LH717'], 'has_flight_segment': True,
         'routing_iatas': ['HND', 'FRA'], 'overnight_after_day': True},
    ]
    out, audit = ci.reconcile_cas_days(cas_bytes, llm_days, 'FRA', force=True)
    assert audit['applied'] is True, audit
    assert audit['parser_confidence'] == 'high'
    by = {d['datum']: d for d in out}
    # LH418 wird als Flug erkannt + überschrieben
    assert by['2025-01-14'].get('flight_numbers') == ['LH418'], by['2025-01-14']
    assert by['2025-01-14'].get('has_flight_segment') is True
    # LH419 Nachtflug-Heimkehr → overnight_after_day auf True korrigiert (VMA)
    assert by['2025-01-15'].get('overnight_after_day') is True
    # tz-Flags gesetzt
    assert by['2025-01-15'].get('tz_overnight_vma') is True
    assert by['2025-01-15'].get('tz_hotel_night') is False
    assert audit['corrections_count'] >= 2


@pytest.mark.skipif(not _HAVE_PDF, reason='Echte CAS-PDF nicht vorhanden')
def test_real_pdf_reconcile_is_deterministic():
    with open(REAL_PDF, 'rb') as f:
        cas_bytes = f.read()
    llm = [{'datum': '2025-01-15', 'flight_numbers': ['LH419'],
            'routing_iatas': ['IAD', 'FRA'], 'overnight_after_day': False}]
    o1, _ = ci.reconcile_cas_days(cas_bytes, [dict(d) for d in llm], 'FRA', force=True)
    o2, _ = ci.reconcile_cas_days(cas_bytes, [dict(d) for d in llm], 'FRA', force=True)
    assert o1[0].get('overnight_after_day') == o2[0].get('overnight_after_day')
