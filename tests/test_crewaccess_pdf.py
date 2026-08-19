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

# Published-Roster-Variante (Juni-Beispiel): Header „Published Roster",
# Leg-Tags (ALT_F/LCK) vor der Position, Voll-Layover-Tag „Layover: MAN".
SYN_PUBLISHED = """Published Roster
Planning period: June 2026
MUSTER, Test Crew
Rank: FO Base: MUC
Date Report (UTC) Tags Pos Activity From To Start (UTC) End (UTC) A/C Layover Trip ID
03 Wed 03:15 ALT_F FO 1972 MUC CGN 04:39 05:46 32N 828921
ALT_F FO 1973 CGN MUC 06:35 07:42 32N
04 Thu 08:40 LCK FO 1938 MUC BER 10:11 11:15 32N 02046
10 Wed 11:25 FO 2504 MUC MAN 20:10 22:17 32N 30:13
11 Thu Layover: MAN
12 Fri 04:55 FO 2505 MAN MUC 06:26 08:17 32N
Created 26Jun2026 21:07 (UTC) by 000000X 1 ( 1)
"""


def test_published_roster_with_tags_and_layover_day():
    ics, err = backend._crewaccess_text_to_ics(SYN_PUBLISHED, carrier='VL')
    assert err is None
    events = backend._parse_ics_to_events(ics)
    secs = backend._build_ical_sectors(events)
    # Tags (ALT_F/LCK) vor der Position duerfen den Leg-Parse nicht brechen:
    assert [s['flight'] for s in secs['2026-06-03']] == ['VL1972', 'VL1973']
    assert [s['flight'] for s in secs['2026-06-04']] == ['VL1938']
    # Voll-Layover-Tag als LAYOVER-Event (LH-Feed-Vokabular):
    lay = [e for e in events if e['summary'] == 'Layover MAN']
    assert len(lay) == 1 and lay[0]['start'] == '2026-06-11'


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


def test_parser_uses_printed_weekday_for_next_month_carry_out():
    """CrewAccess appends the first day of the following month.  The printed
    weekday must keep that row out of day 01 of the planning month."""
    text = """Roster Preview
Planning period: September 2026
Date Report (UTC) Tags Pos Activity From To Start (UTC) End (UTC) A/C Layover Trip ID
30 Wed 11:40 JC 1144 FRA BIO 13:00 15:10 32N 834204
01 Thu 07:50 JC 1087 MRS FRA 08:50 10:35 32N
Created 16Aug2026 16:17 (UTC) by 000000X 1 ( 1)
"""
    ics, err = backend._crewaccess_text_to_ics(text, carrier='VL')
    assert err is None
    assert 'DTSTART:20260930T130000Z' in ics
    assert 'DTSTART:20261001T085000Z' in ics
    assert 'DTSTART:20260901T085000Z' not in ics


def test_parser_accepts_complete_printed_previous_month():
    """Some CrewAccess exports prefix every day of the previous month.

    The repeated day numbers are unambiguous because the PDF also prints the
    weekday.  A September 2026 roster therefore resolves ``01 Sat`` to August
    and the later ``01 Tue`` to September without guessing.
    """
    text = """Roster Preview
Planning period: September 2026
Date Report (UTC) Tags Pos Activity From To Start (UTC) End (UTC) A/C Layover Trip ID
01 Sat NF
31 Mon NF
01 Tue O
07 Mon 13:20 FAM_C EC 1870 MUC FCO 14:40 16:15 32N 01833
Created 16Aug2026 16:17 (UTC) by 000000X 1 ( 1)
"""
    ics, err = backend._crewaccess_text_to_ics(text, carrier='VL')
    assert err is None
    assert 'DTSTART;VALUE=DATE:20260801' in ics
    assert 'DTSTART;VALUE=DATE:20260831' in ics
    assert 'DTSTART;VALUE=DATE:20260901' in ics
    assert 'DTSTART:20260907T144000Z' in ics


def test_parser_rejects_contradictory_printed_weekday():
    text = """Roster Preview
Planning period: September 2026
Date Report (UTC) Tags Pos Activity From To Start (UTC) End (UTC) A/C Layover Trip ID
30 Fri 11:40 JC 1144 FRA BIO 13:00 15:10 32N 834204
Created 16Aug2026 16:17 (UTC) by 000000X 1 ( 1)
"""
    ics, err = backend._crewaccess_text_to_ics(text, carrier='VL')
    assert ics is None and err == 'invalid_roster_day'


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
    Quelle als pdf (url leer).

    GEÄNDERT 31.07.: `import_calendar_feed` schreibt nicht mehr für JEDEN
    Direkt-ICS-Import hart 'pdf' (das log für FlightOps und den Geräte-Abruf),
    sondern das, was der Aufrufer angibt. Dieser Test spielt den PDF-Pfad
    nach — also reicht er `source` genauso mit, wie `import_roster_pdf` es
    real tut. Ohne Angabe wäre 'ics_direct' (Geräte-Abruf) die ehrliche
    Antwort."""
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
    with backend.app.test_request_context(json={'ics_text': ics,
                                                 'source': 'pdf'}):
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


# ── Released Roster (Crew Access / Jeppesen-Export, LHX seit 2026-07) ────────
# Format wie Preview/Published (Zeiten UTC), aber Header „Released Roster".
# Eigenheiten (Alex/Vanessa-Meldungen 2026-07-27): Übernacht-Legs, die LOKAL
# nach Mitternacht ankommen, stehen auf ZWEI Zeilen (Opener From+Start /
# Closer To+End unter der FOLGE-Tageszeile — die ist Lokal-Anzeige, kein
# UTC-Datum); zweiter Dienst am selben Kalendertag beginnt auf einer
# UNDATIERTEN Zeile mit der Report-Zeit; Layover-Spalte trägt die
# Nightstop-Dauer hinter dem letzten Leg.
SYN_RELEASED = """Released Roster
Planning period: August 2026
MUSTER, Test Crew
Rank: JC Base: FRA
Medical: MED (08Mar2031) Qualifications: A320
Recency - Based on 26JUL2026 08:59
Aircraft qualification Months Remaining Last Legal
A320 6 190 31Jan2027
Date Report (UTC) Tags Pos Activity From To Start (UTC) End (UTC) A/C Layover Trip ID
01 Sat SBYL FRA 09:00 19:00
02 Sun OW
09 Sun 11:40 JC 1144 FRA BIO 13:00 15:10 32N 832259
JC 1145 BIO FRA 15:50 18:00 32N
JC 852 FRA 19:10 32N
10 Mon JC 852 HEL 21:40 32N 11:45
09:50 JC 849 HEL FRA 10:50 13:25 32N
JC 194 FRA BER 14:45 15:55 32N
JC 195 BER FRA 16:45 17:55 32N
19 Wed 12:05 FAM_C JC 2518 FRA DUB 13:25 15:55 32N 831846
FAM_C JC 2519 DUB FRA 16:45 19:05 32N
FAM_C JC 2504 FRA MAN 20:10 22:15 32N 30:15
20 Thu Layover: MAN
21 Fri 04:55 JC 2505 MAN FRA 05:55 07:55 32N
31 Mon O
01Aug-31Aug2026 Jan - Aug
OFF Days 12 40
Block time 72:10
Created 26Jul2026 08:59 (UTC) by 000000X 1 ( 1)
"""


def test_released_roster_header_accepted():
    """Vanessa (LHX/MUC): Crew Access liefert seit Jeppesen nur noch
    „Released Roster" — der Header muss akzeptiert werden."""
    ics, err = backend._crewaccess_text_to_ics(SYN_RELEASED, carrier='VL')
    assert err is None and ics
    assert 'Standby FRA' in ics
    assert ics.count('Off Day') == 2
    # Fußzeilen (OFF Days / Block time / Periode) erzeugen KEINE Events:
    assert 'OFF Days' not in ics and 'Block' not in ics


def test_released_overnight_split_leg_paired():
    """Alex (LHX/FRA): Leg mit Lokal-Ankunft nach Mitternacht steht auf zwei
    Zeilen (Opener „JC 852 FRA 19:10" / Closer „JC 852 HEL 21:40" unter der
    Folge-Tageszeile). Beide Zeiten sind UTC UND am Opener-Tag — die
    Closer-Tageszeile ist CrewAccess' Lokal-Anzeige und darf die
    Zeitrechnung nicht verschieben."""
    ics, err = backend._crewaccess_text_to_ics(SYN_RELEASED, carrier='VL')
    assert err is None
    events = backend._parse_ics_to_events(ics)
    secs = backend._build_ical_sectors(events)
    assert [s['flight'] for s in secs['2026-08-09']] == [
        'VL1144', 'VL1145', 'VL852']
    hel = secs['2026-08-09'][2]
    assert (hel['from'], hel['to']) == ('FRA', 'HEL')
    assert hel['dep_iso'] == '2026-08-09T19:10:00Z'
    assert hel['arr_iso'] == '2026-08-09T21:40:00Z'
    # Zweiter Dienst am 10. (undatierte Report-Zeile) verliert keine Legs:
    assert [s['flight'] for s in secs['2026-08-10']] == [
        'VL849', 'VL194', 'VL195']


def test_released_layover_column_synthesises_nightstop():
    """Layover-Spalte (<24 h) ⇒ timed LAYOVER-Event mit LOCATION über die
    echte Bodenzeit — die 2-Tages-Tour hängt damit zusammen. ≥24 h hat schon
    die „Layover: XXX"-Tageszeile → KEIN Duplikat."""
    ics, err = backend._crewaccess_text_to_ics(SYN_RELEASED, carrier='VL')
    assert err is None
    events = backend._parse_ics_to_events(ics)
    lays = [e for e in events if (e.get('summary') or '').strip() == 'LAYOVER']
    assert len(lays) == 1
    assert lays[0]['location'] == 'HEL'
    assert lays[0]['start_iso'] == '2026-08-09T21:40:00Z'
    assert lays[0]['end_iso'] == '2026-08-10T09:25:00Z'   # 21:40 + 11:45
    # MAN-Nightstop (30:15) bleibt allein bei der Tages-Zeile:
    man = [e for e in events if (e.get('summary') or '') == 'Layover MAN']
    assert len(man) == 1 and man[0]['start'] == '2026-08-20'


def test_released_report_becomes_briefing_token():
    """Alex: Briefing-Zeit stand im Roster (Report 11:40 UTC = 13:40 LT FRA,
    80 min vor Abflug), die App zeigte den 45-min-Default. Der Report wandert
    jetzt als Briefing-Token ins erste Leg des Dienstes — auch für den
    zweiten Dienst nach dem Nightstop (Report auf undatierter Zeile,
    Stations-TZ HEL = UTC+3)."""
    ics, err = backend._crewaccess_text_to_ics(SYN_RELEASED, carrier='VL')
    assert err is None
    assert '13:40 LT Briefing FRA · VL1144 FRA - BIO' in ics
    assert '12:50 LT Briefing HEL · VL849 HEL - FRA' in ics
    # Folge-Legs desselben Dienstes tragen KEIN Token:
    assert 'Briefing FRA · VL1145' not in ics
    assert 'Briefing FRA · VL194' not in ics
    # FAM_C-Tag vor der Position bricht das Token nicht (19 Wed, 12:05 UTC
    # = 14:05 LT FRA):
    assert '14:05 LT Briefing FRA · VL2518 FRA - DUB' in ics


def test_preview_report_briefing_token_too():
    """Auch das bestehende Preview-Format bettet den Report jetzt ein
    (05:15 UTC = 07:15 LT MUC im August)."""
    ics, err = backend._crewaccess_text_to_ics(SYN_TEXT, carrier='VL')
    assert err is None
    assert '07:15 LT Briefing MUC · VL2460 MUC - HEL' in ics


def test_released_pipeline_briefing_start_iso():
    """E2E durch die Import-Pipeline: der Briefing-Token wird via
    _corrected_briefing_start_iso zur ical_start_iso-Dienstbeginn-Zeit
    (13:40 LT FRA = 11:40 UTC)."""
    token = 'AT-TEST-CREWACCESS-REL-1'
    for p in (backend._user_profile_path(token),
              os.path.join(backend._USER_HISTORY_DIR, 'briefings', f'{token}.json')):
        try:
            os.remove(p)
        except OSError:
            pass
    ics, err = backend._crewaccess_text_to_ics(SYN_RELEASED, carrier='VL')
    assert err is None
    with backend.app.test_request_context(json={'ics_text': ics}):
        rv = backend.import_calendar_feed(token)
    resp, status = (rv if isinstance(rv, tuple) else (rv, 200))
    assert status == 200 and resp.get_json()['ok'] is True
    briefs = backend._ical_briefings_load(token) or {}
    day = briefs.get('2026-08-09') or {}
    # Nightstop-Ableitung aus dem synthetisierten LAYOVER-Event:
    assert day.get('ical_layover_ort') == 'HEL'
    # Der Token bleibt im persistierten Summary; die LT→UTC-Auflösung passiert
    # zur LESE-Zeit (get_briefings → _corrected_briefing_start_iso) — hier der
    # gleiche Aufruf: 13:40 LT FRA (Sommerzeit) = 11:40 UTC.
    assert '13:40 LT Briefing FRA' in (day.get('ical_summary') or '')
    fixed = backend._corrected_briefing_start_iso(
        '2026-08-09', day.get('ical_summary'), day.get('ical_start_iso'),
        day.get('ical_end_iso'), day_briefing=day)
    assert fixed == '2026-08-09T11:40:00Z'
