"""Discover-Airlines natives „Roster"-PDF (myTime-Export) → ICS-Import.

Anders als das CrewAccess-„Roster Preview" (UTC) trägt dieses Format
Stations-ORTSZEIT („All times local") — der Parser
(`_discover_roster_text_to_ics`) konvertiert via airport_tz nach Wire-UTC.
Format-Eigenheiten, alle im Fixture abgedeckt: Trip-ID-Zeilenumbrüche,
Übernacht-Legs auf zwei Zeilen (Closer mit UND ohne eigene Datumszeile),
Air-Return (TPA→TPA), Zwischenstopp-Kette über den Seitenumbruch,
Bid-Sterne, SBY/PREP, Tages-Marker (OT/O/U/AU/BOT/„---").
Fixture ist SYNTHETISCH (Struktur wie die echten Discover-Beispiele
2026-05/06/07, keine Personendaten).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend

SYN_TEXT = """Roster
Period: May 2026
Crew member: XXX, Muster, Test
Rank: CM Base: FRA
Passports: DE (21May2030) Medical: MED (06Sep2027) Line check: Missing Qualifications: A320, A330
All times local
Date Report Release Tags Pos Activity From To Start End A/C Layover Trip ID Flight Duty Rest
* 01 Fri 11:45 16:17 CM * 050 FRA PHL 13:21 15:47 333 48:08 20260501_50 8:26 10:32 14:20
_006
* 02 Sat * Layover: PHL
* 03 Sun 16:25 CM * 051 PHL 19:12 332
* 04 Mon 09:34 CM * 051 FRA 09:04 332 7:52 11:09 44:26
05 Tue OT
06 Wed O
07 Thu SBY FRA 05:00 17:00 12:00 12:00
08 Fri CM SBY FRA 05:00 10:00 11151
10:00 14:38 CM 052 FRA MSP 11:56 14:08 333 72:02 9:12 16:38 14:30
09 Sat Layover: MSP
15 Fri AU
16 Sat 10:00 17:04 FDP ext. CM 064 FRA TPA 12:28 16:34 333 25:46 20260516_64 10:06 13:04 15:04
14:00 _270
17 Sun 18:50 CM 065 TPA TPA 21:06 21:38 333 0:32
CM 065 TPA 21:54 333
13:41 CM 065 FRA 13:11 333 9:17 12:51 40:19
21 Thu PREP FRA 09:00 17:00 8:00 12:00
23 Tue ---
25 Mon U
26 Tue BOT
Created 21Jul2026 09:58 (UTC) by 000000X 1 ( 2)
Roster
Period: May 2026
Date Report Release Tags Pos Activity From To Start End A/C Layover Trip ID Flight Duty Rest
27 Wed 18:05 CM 136 FRA 19:37 333 20260527_13
6_122
28 Thu CM 136 MBA 05:12 333 8:35
07:45 CM 136 MBA JRO 06:20 07:15 333 23:55 0:55 12:40 15:30
29 Fri Layover: JRO
30 Sat 07:40 CM 137 JRO MBA 08:39 09:24 333 0:45
19:30 CM 137 MBA FRA 10:56 19:00 333 9:04 12:50 12:50
Monthly Jan - May Planned in year
OFF Days 9 53
Remaining OT Days 45
Flight Time BH 62:26 301:42
Created 21Jul2026 09:58 (UTC) by 000000X 2 ( 2)
"""


def test_crewaccess_parser_rejects_discover_format():
    """Dispatch-Voraussetzung: der CrewAccess-Parser lehnt das native
    Discover-Format sauber ab (kein 'Roster Preview'/'Planning period')."""
    ics, err = backend._crewaccess_text_to_ics(SYN_TEXT, carrier='4Y')
    assert ics is None and err == 'unsupported_pdf_format'


def test_discover_parser_rejects_foreign_text():
    ics, err = backend._discover_roster_text_to_ics('Irgendein anderes Dokument')
    assert ics is None and err == 'unsupported_pdf_format'
    # CrewAccess-Text ist für den Discover-Parser fremd (anderer Tabellenkopf):
    ics2, err2 = backend._discover_roster_text_to_ics(
        'Roster Preview\nPlanning period: August 2026\n'
        'Date Report (UTC) Tags Pos Activity From To Start (UTC) End (UTC)')
    assert ics2 is None and err2 == 'unsupported_pdf_format'
    ics3, err3 = backend._discover_roster_text_to_ics(
        'Roster\nPeriod: Zeugnis 2026\n' + backend._DISCOVER_HEADER)
    assert ics3 is None and err3 == 'no_planning_period'


def test_local_times_become_utc():
    """„All times local" → Wire-UTC via airport_tz (FRA=UTC+2, PHL=UTC-4 im Mai)."""
    ics, err = backend._discover_roster_text_to_ics(SYN_TEXT)
    assert err is None
    # Führende Null der Activity wird gestrippt: 050 → 4Y50.
    assert '4Y50 FRA - PHL' in ics
    assert '4Y050' not in ics
    # FRA 13:21 lokal = 11:21Z; PHL 15:47 lokal = 19:47Z (= 8:26 Block wie PDF).
    assert 'DTSTART:20260501T112100Z' in ics
    assert 'DTEND:20260501T194700Z' in ics
    # Standby FRA 05:00 lokal = 03:00Z.
    assert 'Standby FRA' in ics
    assert 'DTSTART:20260507T030000Z' in ics


def test_overnight_closer_with_dated_row():
    """Übernacht-Leg auf zwei Zeilen, Closer MIT Datumszeile (03→04)."""
    ics, err = backend._discover_roster_text_to_ics(SYN_TEXT)
    assert err is None
    # PHL 19:12 lokal (03.) = 23:12Z; FRA 09:04 lokal (04.) = 07:04Z (= 7:52).
    assert '4Y51 PHL - FRA' in ics
    assert 'DTSTART:20260503T231200Z' in ics
    assert 'DTEND:20260504T070400Z' in ics


def test_overnight_closer_without_date_row():
    """Der Ankunftstag kann im Text KOMPLETT fehlen (echtes Beispiel: „18 Mon"
    existiert nicht als Zeile) — Ankunftsdatum via „Ende ≤ Start → +1 Tag"."""
    ics, err = backend._discover_roster_text_to_ics(SYN_TEXT)
    assert err is None
    # TPA 21:54 lokal (17.) = 01:54Z am 18.; FRA 13:11 lokal = 11:11Z am 18.
    assert '4Y65 TPA - FRA' in ics
    assert 'DTSTART:20260518T015400Z' in ics
    assert 'DTEND:20260518T111100Z' in ics
    # Der Air-Return davor bleibt als eigenes Event erhalten:
    assert '4Y65 TPA - TPA' in ics


def test_intermediate_stop_chain_across_pagebreak():
    """136 FRA→MBA→JRO: Opener auf Seite 1, Closer + Folge-Leg nach dem
    Seitenkopf von Seite 2 — pending überlebt den Umbruch."""
    ics, err = backend._discover_roster_text_to_ics(SYN_TEXT)
    assert err is None
    events = backend._parse_ics_to_events(ics)
    secs = backend._build_ical_sectors(events)
    # FRA 19:37 lokal (27.) = 17:37Z; MBA 05:12 lokal (28.) = 02:12Z (= 8:35).
    d27 = secs.get('2026-05-27') or []
    assert [(s['flight'], s['from'], s['to']) for s in d27] == [('4Y136', 'FRA', 'MBA')]
    assert d27[0]['dep_iso'] == '2026-05-27T17:37:00Z'
    assert d27[0]['arr_iso'] == '2026-05-28T02:12:00Z'
    d28 = secs.get('2026-05-28') or []
    assert [(s['flight'], s['from'], s['to']) for s in d28] == [('4Y136', 'MBA', 'JRO')]
    d30 = secs.get('2026-05-30') or []
    assert [(s['flight'], s['from'], s['to']) for s in d30] == [
        ('4Y137', 'JRO', 'MBA'), ('4Y137', 'MBA', 'FRA')]


def test_day_markers_and_continuation_lines():
    ics, err = backend._discover_roster_text_to_ics(SYN_TEXT)
    assert err is None
    assert 'SUMMARY:Off Day' in ics            # O
    assert 'SUMMARY:Urlaub' in ics             # U
    assert 'SUMMARY:OT' in ics                 # unbekannt → roh
    assert 'SUMMARY:BOT' in ics
    assert 'SUMMARY:AU' in ics
    assert 'SUMMARY:PREP FRA' in ics
    assert 'Layover PHL' in ics and 'Layover MSP' in ics and 'Layover JRO' in ics
    assert 'DTSTART;VALUE=DATE:20260502' in ics
    # „---" (leerer Platzhalter) und Trip-ID-Umbrüche erzeugen KEINE Events:
    assert 'SUMMARY:---' not in ics
    assert '_006' not in ics and '6_122' not in ics
    # Summary-Seite (Monthly/OFF Days/…) erzeugt keine Events:
    assert 'OFF Days' not in ics


def test_pipeline_roundtrip_sectors_and_briefings():
    ics, err = backend._discover_roster_text_to_ics(SYN_TEXT)
    assert err is None
    events = backend._parse_ics_to_events(ics)
    secs = backend._build_ical_sectors(events)
    # Turnaround-Tag mit SBY davor: beide Legs als Sektoren am 08.:
    d8 = secs.get('2026-05-08') or []
    assert [(s['flight'], s['from'], s['to']) for s in d8] == [('4Y52', 'FRA', 'MSP')]
    # Übernacht-Sektoren keyen auf den UTC-Abflugtag:
    assert '2026-05-01' in secs and '2026-05-03' in secs
    # Marker-Tage tragen keine Sektoren:
    assert '2026-05-05' not in secs
    assert '2026-05-25' not in secs


def test_endpoint_dispatch_via_real_pdf_upload():
    """E2E: echtes (reportlab-)PDF im Discover-Format → Endpoint erkennt das
    Format hinter dem CrewAccess-First-Dispatch, Antwort trägt source=pdf +
    period aus „Period:"."""
    import io
    from unittest.mock import patch
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 820
    for ln in SYN_TEXT.splitlines():
        if y < 40:
            c.showPage()
            y = 820
        c.drawString(30, y, ln)
        y -= 12
    c.save()
    pdf_bytes = buf.getvalue()

    token = 'AT-TEST-DISCOVER-PDF-1'
    for p in (backend._user_profile_path(token),
              os.path.join(backend._USER_HISTORY_DIR, 'briefings', f'{token}.json')):
        try:
            os.remove(p)
        except OSError:
            pass
    client = backend.app.test_client()
    valid = backend._TokenValidationResult(
        backend._TokenValidationState.VALID, 'discover-pdf@example.test')
    with patch.object(backend, '_validate_token', return_value=valid), \
            patch.object(backend, '_BUG004_REQUIRE_TOKEN_BINDING', False):
        rv = client.post(f'/api/user/roster-pdf/{token}/import',
                         data={'pdf': (io.BytesIO(pdf_bytes), 'roster.pdf'),
                               'airline': 'Discover'},
                         content_type='multipart/form-data')
    payload = rv.get_json() or {}
    assert rv.status_code == 200, payload
    assert payload.get('ok') is True
    assert payload.get('source') == 'pdf'
    assert payload.get('period') == 'May 2026'
    assert payload.get('briefings_imported', 0) >= 5
