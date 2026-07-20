"""CrewAccess-Roster-PDF-Import (Lufthansa City VL / Discover 4Y — kein iCal).

Der Parser (`_crewaccess_text_to_ics`) macht aus dem „Roster Preview"-Text ein
synthetisches ICS; `import_calendar_feed` nimmt es über den `ics_text`-
Direktpfad durch die EINE bestehende Pipeline. Fixture ist SYNTHETISCH
(Format wie das echte City-Beispiel, keine Personendaten).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend

SYN_TEXT = """Roster Preview
Planning period: August 2026
MUSTER, Test Crew
Rank: FO Base: MUC
Recency - Based on 16JUL2026 16:19
Aircraft qualification Days Remaining Last Legal
A320 90 89 12Oct2026
Date Report (UTC) Tags Pos Activity From To Start (UTC) End (UTC) A/C Layover Trip ID
01 Sat O
02 Sun OW
03 Mon 05:15 FO 2460 MUC HEL 06:35 09:05 32N 831580
FO 2461 HEL MUC 09:50 12:25 32N
FO 2502 MUC MAN 14:00 16:05 31D 12:30
04 Tue 05:00 FO 2505 MAN MUC 06:00 08:00 32N
12 Wed RES10
19 Wed SBYL MUC 09:00 19:00
23 Sun 20:25 FO 1906 MUC CTA 21:45 00:10 32N 17:35 831392
29 Sat U
Created 16Jul2026 16:19 (UTC) by 000000X 1 ( 1)
"""


def test_parser_builds_ics_with_all_day_types():
    ics, err = backend._crewaccess_text_to_ics(SYN_TEXT, carrier='VL')
    assert err is None
    assert 'VL2460 MUC - HEL' in ics
    assert 'VL2505 MAN MUC' not in ics          # Summary hat „FROM - TO"-Form
    assert 'VL2505 MAN - MUC' in ics
    assert ics.count('Off Day') == 2            # O + OW
    assert 'Urlaub' in ics
    assert 'Reserve' in ics
    assert 'Standby MUC' in ics
    # Zeiten UTC, Tag 3 erster Leg:
    assert 'DTSTART:20260803T063500Z' in ics
    # All-Day-Marker als VALUE=DATE:
    assert 'DTSTART;VALUE=DATE:20260801' in ics


def test_parser_red_eye_leg_crosses_midnight():
    ics, err = backend._crewaccess_text_to_ics(SYN_TEXT, carrier='VL')
    assert err is None
    # 23.: 21:45 → 00:10 landet am 24. (Ende +1 Tag)
    assert 'DTSTART:20260823T214500Z' in ics
    assert 'DTEND:20260824T001000Z' in ics


def test_parser_pipeline_roundtrip_sectors():
    ics, err = backend._crewaccess_text_to_ics(SYN_TEXT, carrier='VL')
    assert err is None
    events = backend._parse_ics_to_events(ics)
    secs = backend._build_ical_sectors(events)
    d3 = secs.get('2026-08-03') or []
    assert [s['flight'] for s in d3] == ['VL2460', 'VL2461', 'VL2502']
    assert d3[0]['from'] == 'MUC' and d3[0]['to'] == 'HEL'
    assert d3[0]['dep_iso'] == '2026-08-03T06:35:00Z'
    # Marker-Tage tragen keine Sektoren:
    assert '2026-08-01' not in secs
    assert '2026-08-12' not in secs


def test_parser_discover_prefix():
    ics, err = backend._crewaccess_text_to_ics(SYN_TEXT, carrier='4Y')
    assert err is None
    assert '4Y2460 MUC - HEL' in ics


def test_parser_rejects_foreign_pdf_text():
    ics, err = backend._crewaccess_text_to_ics('Irgendein anderes Dokument', carrier='VL')
    assert ics is None and err == 'unsupported_pdf_format'
    ics2, err2 = backend._crewaccess_text_to_ics(
        'Roster Preview\nPlanning period: Zeugnis 2026', carrier='VL')
    assert ics2 is None and err2 == 'no_planning_period'


def test_carrier_mapping():
    assert backend._crewaccess_carrier_for('Discover', 'AT-X') == '4Y'
    assert backend._crewaccess_carrier_for('Lufthansa City', 'AT-X') == 'VL'
    assert backend._crewaccess_carrier_for('', 'AT-UNKNOWN-TOKEN') == 'VL'


def test_import_endpoint_ics_text_direct_path():
    """`ics_text` läuft ohne Fetch durch die volle Pipeline und markiert die
    Quelle als pdf (url leer)."""
    token = 'AT-TEST-CREWACCESS-1'
    # IDEMPOTENZ: Disk-State früherer Läufe räumen — briefings_imported zählt
    # nur NEUE Tage; ohne Cleanup wäre der zweite Lauf 0 (Suite-Ordnungs-Rot).
    for p in (backend._user_profile_path(token),
              os.path.join(backend._USER_HISTORY_DIR, 'briefings', f'{token}.json')):
        try:
            os.remove(p)
        except OSError:
            pass
    ics, err = backend._crewaccess_text_to_ics(SYN_TEXT, carrier='VL')
    assert err is None
    with backend.app.test_request_context(json={'ics_text': ics}):
        rv = backend.import_calendar_feed(token)
    resp, status = (rv if isinstance(rv, tuple) else (rv, 200))
    payload = resp.get_json()
    assert status == 200 and payload['ok'] is True
    assert payload['events_count'] >= 7
    assert payload['briefings_imported'] >= 6
    # Quelle im calendar_feed-Slot: pdf, url leer.
    prof = backend._profile_load(token) or {}
    feed = ((prof.get('profile') or {}).get('calendar_feed')
            or prof.get('calendar_feed') or {})
    assert feed.get('source') == 'pdf'
    assert feed.get('url') == ''
