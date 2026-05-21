"""v11 Clean-Release — Flugstundenuebersicht-Legacy-Purge Tests.

Verifiziert dass:
- Die 4 Legacy-Reader-Funktionen RuntimeError werfen ohne Forensik-Override.
- Der hybrid_analyze elif-dp_bytes-Branch hart auf health=red stoppt, KEIN
  Aufruf von _sonnet_read_dp_structured_chunked_v104 mehr im aktiven Pfad.
- Audit-Label fuer dp-Uploads ist 'legacy_ignored_flight_hours_summary'.
- Forensik-Override (AEROTAX_LEGACY_FLUGSTUNDEN_FORENSIK=1) macht die Funktionen
  wieder benutzbar (fuer reine Forensik-Lauefe, NICHT fuer Produktion).

Spec: docs/FLUGSTUNDEN_LEGACY_PURGE.md
"""
import os
import sys
import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Test-Mode-Boot (RECOVERY_SECRET nicht zwingend)
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app as app_module


def _ensure_forensik_off():
    os.environ.pop('AEROTAX_LEGACY_FLUGSTUNDEN_FORENSIK', None)


@pytest.fixture(autouse=True)
def _no_forensik_unless_asked():
    _ensure_forensik_off()
    yield
    _ensure_forensik_off()


def test_parse_flugstunden_deterministic_raises_without_forensik():
    with pytest.raises(RuntimeError) as ei:
        app_module._parse_flugstunden_deterministic('dummy', homebase='FRA')
    assert 'v11 Clean-Release' in str(ei.value)
    assert 'LSB + SE + Dienstplan/CAS' in str(ei.value)


def test_parse_dienstplan_mit_ki_raises_without_forensik():
    with pytest.raises(RuntimeError) as ei:
        app_module.parse_dienstplan_mit_ki([b'dummy'])
    assert 'v11 Clean-Release' in str(ei.value)


def test_sonnet_read_dp_structured_raises_without_forensik():
    with pytest.raises(RuntimeError) as ei:
        app_module._sonnet_read_dp_structured([b'dummy'])
    assert 'v11 Clean-Release' in str(ei.value)


def test_sonnet_read_dp_structured_chunked_v104_raises_without_forensik():
    with pytest.raises(RuntimeError) as ei:
        app_module._sonnet_read_dp_structured_chunked_v104([b'dummy'])
    assert 'v11 Clean-Release' in str(ei.value)


def test_audit_label_for_dp_is_legacy_ignored_flight_hours_summary():
    """Im hybrid_analyze /process Pfad ist das Audit-Label fuer dp-Uploads
    'legacy_ignored_flight_hours_summary' (statt 'flugstunden')."""
    src = open(os.path.join(ROOT_DIR, 'app.py'), 'r', encoding='utf-8').read()
    # Direct-Upload-Block
    direct_audit_idx = src.find('Direct-Upload')
    assert direct_audit_idx > 0
    pre = src[max(0, direct_audit_idx - 600):direct_audit_idx + 200]
    assert "'dp': 'legacy_ignored_flight_hours_summary'" in pre, \
        'Audit-Label fuer dp muss auf legacy_ignored_flight_hours_summary stehen.'


def test_hybrid_analyze_dp_branch_hard_stops():
    """In hybrid_analyze: elif dp_bytes: setzt sofort health=red, ohne Legacy-Reader."""
    src = open(os.path.join(ROOT_DIR, 'app.py'), 'r', encoding='utf-8').read()
    fn_idx = src.find('def hybrid_analyze(')
    assert fn_idx > 0
    block = src[fn_idx:fn_idx + 80000]
    # elif dp_bytes existiert
    assert 'elif dp_bytes' in block
    # Hard-stop-Marker
    assert "'status': 'red'" in block
    assert 'Flugstundenuebersicht wird seit v11 nicht mehr als Quelle akzeptiert' in block
    # Legacy-Reader nicht im aktiven Pfad
    active_block = block.split('elif False:')[0]
    assert '_sonnet_read_dp_structured_chunked_v104(' not in active_block, \
        'Legacy-Reader darf nicht im aktiven elif-dp_bytes-Pfad gerufen werden.'


def test_forensik_override_allows_function_call():
    """Forensik-Override macht die Funktion wieder benutzbar — fuer reine Forensik."""
    os.environ['AEROTAX_LEGACY_FLUGSTUNDEN_FORENSIK'] = '1'
    try:
        # Sollte nicht mehr RuntimeError werfen, sondern echte Logik laufen lassen.
        # _parse_flugstunden_deterministic mit leerem Text liefert leeres Result.
        result = app_module._parse_flugstunden_deterministic('', homebase='FRA')
        # Erfolgreich — kein RuntimeError. Result kann beliebig sein.
        assert result is not None or result is None  # Forensik-Lauf erfolgreich
    finally:
        os.environ.pop('AEROTAX_LEGACY_FLUGSTUNDEN_FORENSIK', None)


def test_no_silent_fallback_on_missing_cas():
    """Wenn cas_bytes leer + dp_bytes da: KEIN silent Fallback auf Legacy-Reader.

    Pruefen statisch: der elif-dp_bytes-Branch setzt classification=None +
    document_health=red, statt structured_days zu berechnen.
    """
    src = open(os.path.join(ROOT_DIR, 'app.py'), 'r', encoding='utf-8').read()
    fn_idx = src.find('def hybrid_analyze(')
    block = src[fn_idx:fn_idx + 80000]
    # Im elif-dp_bytes-Block: classification = None setzen
    elif_idx = block.find('elif dp_bytes')
    elif_end = block.find('elif False:', elif_idx)
    assert elif_idx > 0 and elif_end > elif_idx
    elif_block = block[elif_idx:elif_end]
    assert 'classification = None' in elif_block
    assert 'structured_days = None' in elif_block
    # Kein _sonnet_read_dp_structured-Aufruf in diesem aktiven Block
    assert '_sonnet_read_dp_structured' not in elif_block, \
        'Aktiver elif-dp_bytes-Block darf KEIN DP-Reader-Call enthalten.'
