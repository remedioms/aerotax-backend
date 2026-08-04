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
    """„All times local" → Wire-UTC via airport_tz (FRA=UTC+2, PHL=UTC-4 im Mai).
    Legs verwenden DTSTART;TZID=<stn> (lokale Abflugzeit); UTC-Äquivalente werden
    über _parse_ics_to_events / _build_ical_sectors verifiziert."""
    ics, err = backend._discover_roster_text_to_ics(SYN_TEXT)
    assert err is None
    # Führende Null der Activity wird gestrippt: 050 → 4Y50.
    assert '4Y50 FRA - PHL' in ics
    assert '4Y050' not in ics
    events = backend._parse_ics_to_events(ics)
    secs = backend._build_ical_sectors(events)
    # FRA 13:21 lokal (UTC+2 CEST) = 11:21Z; PHL 15:47 lokal (UTC-4 EDT) = 19:47Z (= 8:26 Block wie PDF).
    d1 = secs.get('2026-05-01') or []
    assert [(s['flight'], s['from'], s['to']) for s in d1] == [('4Y50', 'FRA', 'PHL')]
    assert d1[0]['dep_iso'] == '2026-05-01T11:21:00Z'
    assert d1[0]['arr_iso'] == '2026-05-01T19:47:00Z'
    # DTEND für Legs bleibt UTC-Z-Format — einfach prüfbar im ICS-String.
    assert 'DTEND:20260501T194700Z' in ics
    # Standby FRA 05:00 lokal = 03:00Z — SBY/PREP bleiben UTC-Z.
    assert 'Standby FRA' in ics
    assert 'DTSTART:20260507T030000Z' in ics


def test_overnight_closer_with_dated_row():
    """Übernacht-Leg auf zwei Zeilen, Closer MIT Datumszeile (03→04).
    DTSTART wird als TZID=America/New_York-Lokalzeit emittiert; dep_iso und
    arr_iso werden über _build_ical_sectors verifiziert."""
    ics, err = backend._discover_roster_text_to_ics(SYN_TEXT)
    assert err is None
    assert '4Y51 PHL - FRA' in ics
    # DTEND bleibt UTC-Z — direkt prüfbar.
    assert 'DTEND:20260504T070400Z' in ics
    # UTC-Äquivalente via Sector-Pipeline: PHL 19:12 (UTC-4) = 23:12Z; FRA 09:04 (UTC+2) = 07:04Z.
    events = backend._parse_ics_to_events(ics)
    secs = backend._build_ical_sectors(events)
    d3 = secs.get('2026-05-03') or []
    assert [(s['flight'], s['from'], s['to']) for s in d3] == [('4Y51', 'PHL', 'FRA')]
    assert d3[0]['dep_iso'] == '2026-05-03T23:12:00Z'
    assert d3[0]['arr_iso'] == '2026-05-04T07:04:00Z'


def test_overnight_closer_without_date_row():
    """Der Ankunftstag kann im Text KOMPLETT fehlen (echtes Beispiel: „18 Mon"
    existiert nicht als Zeile) — Ankunftsdatum via „Ende ≤ Start → +1 Tag".
    DTSTART wird als TZID=America/New_York-Lokalzeit emittiert (TPA lokal 21:54,
    UTC 01:54Z am 18.); dep_iso = 2026-05-18T01:54:00Z; arr_iso = 11:11Z."""
    ics, err = backend._discover_roster_text_to_ics(SYN_TEXT)
    assert err is None
    assert '4Y65 TPA - FRA' in ics
    # DTEND bleibt UTC-Z — direkt prüfbar.
    assert 'DTEND:20260518T111100Z' in ics
    # DTSTART trägt lokale TPA-Zeit mit TZID (21:54 EDT = 01:54Z am 18.):
    assert 'DTSTART;TZID=America/New_York:20260517T215400' in ics
    # _build_ical_sectors keyed auf start_iso[:10] = UTC-Datum (2026-05-18):
    events = backend._parse_ics_to_events(ics)
    secs = backend._build_ical_sectors(events)
    d18 = secs.get('2026-05-18') or []
    assert any(s.get('flight') == '4Y65' and s.get('from') == 'TPA' and s.get('to') == 'FRA'
               for s in d18), f'4Y65 TPA-FRA not on May 18, secs={list(secs.keys())}'
    tpa_fra = next(s for s in d18 if s.get('flight') == '4Y65' and s.get('to') == 'FRA')
    assert tpa_fra['dep_iso'] == '2026-05-18T01:54:00Z'
    assert tpa_fra['arr_iso'] == '2026-05-18T11:11:00Z'
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


# Echtes Discover-Tabellen-Shape aus den Jun-/Aug-2026-Belegen, anonymisiert.
# Insbesondere wichtig: Fortsetzungszeilen am selben Datum tragen KEIN neues
# Datum; `cur_day` muss deshalb über alle Schulungsblöcke erhalten bleiben.
_GROUND_DUTY_FIXTURE = """\
Roster
Period: August 2026
Crew member: TST, Test, Ground
Rank: CM Base: FRA
All times local
Date Report Release Tags Pos Activity From To Start End A/C Layover Trip ID Flight Duty Rest
04 Tue BRS FRA 10:00 17:00 7:00 12:00
18 Tue CM HOS FRA 09:00 16:00 7:00 12:00
19 Wed CM SEP320 FRA 09:30 12:00 2:30
CM FA FRA 12:00 14:30 2:30
CM SEP330 FRA 14:30 16:45 2:15 15:15
20 Thu CM SEP330 FRA 09:30 12:00 2:30
CM CRM FRA 12:00 14:00 2:00
CM SEC FRA 14:00 16:00 2:00
CM SEP320 FRA 16:00 18:10 2:10 15:20
21 Fri BOT
Created 21Jul2026 09:58 (UTC) by TST 1 ( 1)
"""


def test_discover_ground_duties_are_not_dropped():
    """Alle zeitgebundenen Discover-Bodendienste werden als eigene Events
    übernommen; unbekannte künftige Activity-Codes brauchen keine Whitelist."""
    ics, err = backend._discover_roster_text_to_ics(_GROUND_DUTY_FIXTURE)
    assert err is None, err
    for summary in (
            'BRS FRA', 'HOS FRA', 'SEP320 FRA', 'FA FRA', 'SEP330 FRA',
            'CRM FRA', 'SEC FRA'):
        assert f'SUMMARY:{summary}' in ics
    # August = CEST: 10:00 FRA lokal → 08:00Z; 18:10 → 16:10Z.
    assert 'DTSTART:20260804T080000Z' in ics
    assert 'DTEND:20260804T150000Z' in ics
    assert 'DTEND:20260820T161000Z' in ics
    # Ganztägiges BOT bleibt als Rohcode erhalten und wird im Client als Dienst
    # klassifiziert (keine erfundene Uhrzeit).
    assert 'DTSTART;VALUE=DATE:20260821' in ics
    assert 'SUMMARY:BOT' in ics


def test_multiple_discover_ground_duties_merge_on_the_correct_day():
    ics, err = backend._discover_roster_text_to_ics(_GROUND_DUTY_FIXTURE)
    assert err is None, err
    events = backend._parse_ics_to_events(ics)
    briefings, _ = backend._ics_events_to_briefings(events, existing={})

    aug19 = briefings['2026-08-19']
    assert aug19['ical_summary'] == 'SEP320 FRA · FA FRA · SEP330 FRA'
    assert aug19['ical_start_iso'] == '2026-08-19T07:30:00Z'
    assert aug19['ical_end_iso'] == '2026-08-19T14:45:00Z'

    aug20 = briefings['2026-08-20']
    assert aug20['ical_summary'] == 'SEP330 FRA · CRM FRA · SEC FRA · SEP320 FRA'
    assert aug20['ical_start_iso'] == '2026-08-20T07:30:00Z'
    assert aug20['ical_end_iso'] == '2026-08-20T16:10:00Z'


def test_discover_ground_codes_are_backend_duty_evidence():
    for marker in ('BRS FRA', 'BOT', 'HOS FRA', 'SEP320 FRA', 'SEP330 FRA',
                   'FA FRA', 'CRM FRA', 'SEC FRA', 'PRVT FRA - MUC',
                   'RESERVE'):
        assert backend._summary_has_ground_duty(marker), marker
    assert backend._summary_has_ground_duty('OFF DAY · BOT')


_OPEN_BUGS_DISCOVER_VARIANTS = """\
Released roster
Period: July 2026
Crew member: TST, Test, Variants
Rank: RCM Base: FRA
All times local
Date Report Release Tags Pos Activity From To Start End A/C Layover Trip ID Flight Duty Rest
05 Wed 10:15 RCM PRVT FRA MUC 10:15 11:10 05351
13:25 RCM 1272 MUC JTR 14:40 18:20 32A 2:40
08 Sat 09:05 RCM 442 MUC IBZ 10:20 12:45 32A 2:25
DH LH115 MUC FRA 18:00 19:00 9:55 12:00
13 Thu RES 02:00 22:00
Created 04Aug2026 09:39 (UTC) by TST 1 ( 1)
"""


def test_released_roster_lowercase_header_and_open_bugs_duties():
    """Echte Open-Bugs-Varianten: kleingeschriebener Released-Header,
    PRVT-Transfer, Airline-fremder Deadhead und Reserve ohne Station."""
    ics, err = backend._discover_roster_text_to_ics(
        _OPEN_BUGS_DISCOVER_VARIANTS)
    assert err is None, err
    assert 'SUMMARY:PRVT FRA - MUC' in ics
    assert 'SUMMARY:DH LH115 MUC - FRA' in ics
    assert 'SUMMARY:Reserve' in ics
    # Juli = CEST: 02:00-22:00 FRA lokal → 00:00-20:00 UTC.
    assert 'DTSTART:20260713T000000Z' in ics
    assert 'DTEND:20260713T200000Z' in ics
    # Normaler Flug bleibt trotz vorgelagertem PRVT erhalten.
    assert '4Y1272 MUC - JTR' in ics


def test_ios_reprinted_discover_cid_text_is_repaired_before_parsing():
    """iOS-„Drucken → PDF" verliert die ToUnicode-CMap und pdfplumber gibt
    `(cid:N)` aus. Der echte NotoSans-Offset wird streng format-geprüft
    repariert; anschließend läuft der normale Discover-Parser."""
    plain = _OPEN_BUGS_DISCOVER_VARIANTS.replace('Released roster', 'Roster')

    def cid_encode(value):
        return ''.join(
            ch if ch == '\n' else f'(cid:{ord(ch) - 29})'
            for ch in value
        )

    repaired = backend._repair_ios_reprinted_roster_text(cid_encode(plain))
    assert repaired == plain
    ics, err = backend._discover_roster_text_to_ics(repaired)
    assert err is None, err
    assert 'SUMMARY:PRVT FRA - MUC' in ics
    assert 'SUMMARY:Reserve' in ics


def test_cid_repair_never_mutates_foreign_pdf_text():
    foreign = ''.join(f'(cid:{n})' for n in range(20, 45))
    assert backend._repair_ios_reprinted_roster_text(foreign) == foreign


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



# ── Briefing-Token aus Report-Spalte (Panu-Bug-Fix) ──────────────────────────

# Minimal-Fixture mit den Schlüssel-Fällen aus den echten Jul/Mai-PDFs:
#   • Einfacher Turnaround-Dienst (Jul 24): Report=12:00, Start=13:30 → YYC
#   • Multi-Leg-Tag (Mai 22): Report=10:55, zwei Sektoren → Token nur auf Leg 1
#   • FDP-ext.-Marker (Mai 16): Report=10:00 trotz „FDP ext." Zwischen-Token
#   • Übernacht-Opener (Jul 06): Report=18:05 reist im pending mit → erscheint
#     auf dem ausgeklappten Leg (Closer auf Folgetag)
#   • SBY-then-Flight (Mai 08): Datumszeile ohne HH:MM am Anfang → kein Token
_BRIEFING_FIXTURE = """\
Roster
Period: July 2026
Crew member: TST, Test, Briefing
Rank: CM Base: FRA
Passports: DE (21May2030) Medical: MED (06Sep2027) Line check: Missing Qualifications: A320, A330
All times local
Date Report Release Tags Pos Activity From To Start End A/C Layover Trip ID Flight Duty Rest
24 Fri 12:00 15:35 CM 076 FRA YYC 13:30 15:05 333 24:00 20260724_76_480 9:35 11:35 14:40
25 Sat 15:35 CM 077 YYC 16:50 333
26 Sun 10:45 CM 077 FRA 10:15 333 9:25 11:10 43:15
Created 21Jul2026 09:45 (UTC) by TST 1 ( 1)
"""

_BRIEFING_FIXTURE_MAY = """\
Roster
Period: May 2026
Crew member: TST, Test, Briefing
Rank: CM Base: FRA
Passports: DE (21May2030) Medical: MED (06Sep2027) Line check: Missing Qualifications: A320, A330
All times local
Date Report Release Tags Pos Activity From To Start End A/C Layover Trip ID Flight Duty Rest
08 Fri CM SBY FRA 05:00 10:00 11151
10:00 14:38 CM 052 FRA MSP 11:56 14:08 333 72:02 9:12 16:38 14:30
16 Sat 10:00 17:04 FDP ext. CM 064 FRA TPA 12:28 16:34 333 25:46 20260516_64_270 10:06 13:04 15:04
22 Fri 10:55 CM 518 FRA PMI 12:23 14:12 32A 05863 1:49
17:44 CM 519 PMI FRA 15:09 17:29 32A 2:20 6:49 12:00
Created 21Jul2026 09:58 (UTC) by TST 1 ( 1)
"""

_BRIEFING_FIXTURE_OVERNIGHT = """\
Roster
Period: July 2026
Crew member: TST, Test, Briefing
Rank: CM Base: FRA
Passports: DE (21May2030) Medical: MED (06Sep2027) Line check: Missing Qualifications: A320, A330
All times local
Date Report Release Tags Pos Activity From To Start End A/C Layover Trip ID Flight Duty Rest
06 Mon 18:05 CM 136 FRA 19:37 333 20260706_136_122
07 Tue CM 136 MBA 05:12 333 8:35
07:45 CM 136 MBA JRO 06:20 07:15 333 23:55 0:55 12:40 15:30
Created 21Jul2026 09:45 (UTC) by TST 1 ( 1)
"""


def test_briefing_token_single_turnaround_yyc():
    """Jul-24-Zeile: Report=12:00, Departure=13:30 (FRA→YYC).
    Das ICS-Summary des Legs muss den Briefing-Token „12:00 LT Briefing FRA"
    enthalten, damit iOS briefingTimeFromSummary() 12:00 statt 13:30 zeigt.
    DTSTART wird jetzt als TZID=Europe/Berlin-Lokalzeit emittiert (statt UTC-Z)."""
    ics, err = backend._discover_roster_text_to_ics(_BRIEFING_FIXTURE)
    assert err is None, err
    # Briefing-Token im Summary des FRA→YYC-Legs:
    assert '12:00 LT Briefing FRA' in ics
    assert '4Y76 FRA - YYC' in ics
    # Vollformat: „12:00 LT Briefing FRA · 4Y76 FRA - YYC"
    assert '12:00 LT Briefing FRA · 4Y76 FRA - YYC' in ics
    # DTSTART via TZID=Europe/Berlin in Lokalzeit (13:30 FRA = 11:30Z CEST):
    assert 'DTSTART;TZID=Europe/Berlin:20260724T133000' in ics
    # UTC-Äquivalenz via Sector-Pipeline: dep_iso = 11:30Z.
    events = backend._parse_ics_to_events(ics)
    secs = backend._build_ical_sectors(events)
    d24 = secs.get('2026-07-24') or []
    assert any(s.get('flight') == '4Y76' for s in d24), f'4Y76 not on Jul 24, secs={list(secs.keys())}'
    d24_76 = next(s for s in d24 if s.get('flight') == '4Y76')
    assert d24_76['dep_iso'] == '2026-07-24T11:30:00Z'
    # Departure != Briefing: Briefing-Zeit (12:00 LT = 10:00Z) ≠ dep_iso (11:30Z).
    assert d24_76['dep_iso'] != '2026-07-24T10:00:00Z'


def test_briefing_token_multi_leg_day_first_only():
    """Mai-22: Report=10:55 gilt für den Dienst (FRA→PMI→FRA).
    Nur der ERSTE Leg trägt den Briefing-Token; der Rückflug-Leg darf ihn
    nicht tragen (er hat keinen eigenen Briefing-Eintrag)."""
    ics, err = backend._discover_roster_text_to_ics(_BRIEFING_FIXTURE_MAY)
    assert err is None, err
    # Erster Leg hat Token:
    assert '10:55 LT Briefing FRA · 4Y518 FRA - PMI' in ics
    # Zweiter Leg (PMI→FRA) hat keinen Briefing-Token:
    assert '10:55 LT Briefing PMI' not in ics
    assert '17:44 LT Briefing' not in ics


def test_briefing_token_fdp_ext_row():
    """Mai-16: Zeile mit „FDP ext. HH:MM" zwischen Release und Pos.
    Das Report-Token (10:00) muss trotz des FDP-ext.-Einschubs erkannt werden."""
    ics, err = backend._discover_roster_text_to_ics(_BRIEFING_FIXTURE_MAY)
    assert err is None, err
    assert '10:00 LT Briefing FRA · 4Y64 FRA - TPA' in ics


def test_briefing_token_sby_then_flight_no_report():
    """Mai-08: Datumszeile beginnt mit Pos (CM SBY …), kein HH:MM am Anfang.
    Hier gibt es keinen Report → der Flug-Leg am selben Tag darf KEINEN
    Briefing-Token erhalten (wir erfinden keine Zeit)."""
    ics, err = backend._discover_roster_text_to_ics(_BRIEFING_FIXTURE_MAY)
    assert err is None, err
    # FRA→MSP-Leg ist da:
    assert '4Y52 FRA - MSP' in ics
    # Aber OHNE Briefing-Token:
    assert 'LT Briefing FRA · 4Y52' not in ics


def test_briefing_token_overnight_opener():
    """Jul-06 Übernacht-Opener (FRA→MBA, kein Ankunfts-Airport auf der Zeile):
    Report=18:05 muss beim aufgelösten Leg (MBA als Zwischenstopp→JRO) landen."""
    ics, err = backend._discover_roster_text_to_ics(_BRIEFING_FIXTURE_OVERNIGHT)
    assert err is None, err
    # Der Opener-Leg FRA→MBA trägt den Briefing-Token:
    assert '18:05 LT Briefing FRA' in ics
    assert '18:05 LT Briefing FRA · 4Y136 FRA - MBA' in ics
    # Der Folgetag-Closer (MBA→JRO) ist ein eigenständiger Leg und trägt keinen Token:
    assert 'LT Briefing MBA · 4Y136 MBA - JRO' not in ics


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


# ── Outstation-Origin Overnight Return — Date-Placement Bug (Panu-Regression) ─
#
# Bug: für Outstation-Abflug-Übernachtlegs (z.B. PHL→FRA, YYC→FRA) wurde das
# Briefing-Event auf dem ANKUNFTSTAG platziert statt auf dem ABFLUGTAG.
# Ursache: `_ics_parse_dt` konvertierte UTC-Z-DTSTART nach Europe/Berlin →
# bei Abflügen > 22:00Z rollte die Lokalzeit auf den Folgetag (23:12Z PHL =
# 01:12 Berlin = nächster Kalender-Tag).
# Fix: Discover-Legs emittieren DTSTART;TZID=<AbflugstationTZ>:<Lokalzeit>,
# sodass der Tages-Bucket = Abflugtag der Abflugstation (korrekt).
#
# Confirmed failing cases from panu's real PDFs (26:05.pdf / 26:07.pdf):
#   • May 03: 4Y51 PHL→FRA, Report 16:25 PHL local, dep 19:12 PHL local
#   • Jul 25: 4Y77 YYC→FRA, Report 15:35 YYC local, dep 16:50 YYC local

_OUTSTATION_OVERNIGHT_MAY = """\
Roster
Period: May 2026
Crew member: PNX, Reuter, Panuwat
Rank: CM Base: FRA
Passports: DE (21May2030) Medical: MED (06Sep2027) Line check: Missing Qualifications: A320, A330
All times local
Date Report Release Tags Pos Activity From To Start End A/C Layover Trip ID Flight Duty Rest
* 01 Fri 11:45 16:17 CM * 050 FRA PHL 13:21 15:47 333 48:08 20260501_50
_006
8:26 10:32 14:20
* 02 Sat * Layover: PHL
* 03 Sun 16:25 CM * 051 PHL 19:12 332
* 04 Mon 09:34 CM * 051 FRA 09:04 332 7:52 11:09 44:26
Created 21Jul2026 09:58 (UTC) by U753456 1 ( 1)
"""

_OUTSTATION_OVERNIGHT_JUL = """\
Roster
Period: July 2026
Crew member: PNX, Reuter, Panuwat
Rank: CM Base: FRA
Passports: DE (21May2030) Medical: MED (06Sep2027) Line check: Missing Qualifications: A320, A330
All times local
Date Report Release Tags Pos Activity From To Start End A/C Layover Trip ID Flight Duty Rest
24 Fri 12:00 15:35 CM 076 FRA YYC 13:30 15:05 333 24:00 20260724_76
_480
9:35 11:35 14:40
25 Sat 15:35 CM 077 YYC 16:50 333
26 Sun 10:45 CM 077 FRA 10:15 333 9:25 11:10 43:15
Created 21Jul2026 09:45 (UTC) by U753456 1 ( 1)
"""


def test_outstation_overnight_phl_fra_departs_on_may03():
    """May 03: 4Y51 PHL→FRA — Outstation-Overnight-Opener.
    Report=16:25 PHL local, dep=19:12 PHL local (EDT = UTC-4).
    Das Leg-Event MUSS auf May 03 (Abflugtag) landen, nicht auf May 04.
    start_iso = 2026-05-03T23:12:00Z (19:12 EDT = 23:12Z am 3.), korrekt.
    Briefing-Token „16:25 LT Briefing PHL" muss auf dem May-03-Briefing
    erscheinen (= _corrected_briefing_start_iso ergibt 20:25Z am 3.).
    NICHT REGRESSIEREN: 4Y50 FRA→PHL (May 01) bleibt auf May 01."""
    ics, err = backend._discover_roster_text_to_ics(_OUTSTATION_OVERNIGHT_MAY)
    assert err is None, err
    # Summary vorhanden
    assert '4Y51 PHL - FRA' in ics
    assert '16:25 LT Briefing PHL · 4Y51 PHL - FRA' in ics
    # DTSTART emittiert als TZID=America/New_York Lokalzeit (19:12 local May 03)
    assert 'DTSTART;TZID=America/New_York:20260503T191200' in ics
    # DTEND bleibt UTC-Z (FRA 09:04 CEST = 07:04Z May 04)
    assert 'DTEND:20260504T070400Z' in ics
    # Sektor landet via _build_ical_sectors auf 2026-05-03 (start_iso[:10] = UTC-Datum)
    events = backend._parse_ics_to_events(ics)
    secs = backend._build_ical_sectors(events)
    d3 = secs.get('2026-05-03') or []
    assert any(s.get('flight') == '4Y51' and s.get('from') == 'PHL' and s.get('to') == 'FRA'
               for s in d3), f'4Y51 PHL-FRA not on May 03; secs={list(secs.keys())}'
    leg = next(s for s in d3 if s.get('flight') == '4Y51')
    assert leg['dep_iso'] == '2026-05-03T23:12:00Z', leg['dep_iso']
    assert leg['arr_iso'] == '2026-05-04T07:04:00Z', leg['arr_iso']
    # Briefing-Event auf May 03 (start = local TPA/PHL date = 2026-05-03)
    ev_51 = next((e for e in events if '4Y51 PHL - FRA' in (e.get('summary') or '')), None)
    assert ev_51 is not None
    assert ev_51.get('start') == '2026-05-03', (
        f'Briefing event start={ev_51.get("start")!r}, expected 2026-05-03 (departure day)')
    # Nicht Regression: FRA→PHL-Leg (May 01) bleibt auf May 01
    d1 = secs.get('2026-05-01') or []
    assert any(s.get('flight') == '4Y50' for s in d1), 'FRA-PHL regression: not on May 01'


def test_outstation_overnight_yyc_fra_departs_on_jul25():
    """Jul 25: 4Y77 YYC→FRA — Outstation-Overnight-Opener.
    Report=15:35 YYC local, dep=16:50 YYC local (MDT = UTC-6).
    Das Leg-Event MUSS auf Jul 25 (Abflugtag) landen, nicht auf Jul 26.
    start_iso = 2026-07-25T22:50:00Z (16:50 MDT = 22:50Z am 25.), korrekt.
    NICHT REGRESSIEREN: 4Y76 FRA→YYC (Jul 24) bleibt auf Jul 24."""
    ics, err = backend._discover_roster_text_to_ics(_OUTSTATION_OVERNIGHT_JUL)
    assert err is None, err
    assert '4Y77 YYC - FRA' in ics
    assert '15:35 LT Briefing YYC · 4Y77 YYC - FRA' in ics
    # DTSTART als TZID=America/Edmonton Lokalzeit (16:50 local Jul 25)
    assert 'DTSTART;TZID=America/Edmonton:20260725T165000' in ics
    # DTEND bleibt UTC-Z (FRA 10:15 CEST = 08:15Z Jul 26)
    assert 'DTEND:20260726T081500Z' in ics
    # Sektor auf 2026-07-25 (start_iso[:10] = UTC-Datum = 2026-07-25)
    events = backend._parse_ics_to_events(ics)
    secs = backend._build_ical_sectors(events)
    d25 = secs.get('2026-07-25') or []
    assert any(s.get('flight') == '4Y77' and s.get('from') == 'YYC' and s.get('to') == 'FRA'
               for s in d25), f'4Y77 YYC-FRA not on Jul 25; secs={list(secs.keys())}'
    leg = next(s for s in d25 if s.get('flight') == '4Y77')
    assert leg['dep_iso'] == '2026-07-25T22:50:00Z', leg['dep_iso']
    assert leg['arr_iso'] == '2026-07-26T08:15:00Z', leg['arr_iso']
    # Briefing-Event auf Jul 25 (start = local YYC date = 2026-07-25)
    ev_77 = next((e for e in events if '4Y77 YYC - FRA' in (e.get('summary') or '')), None)
    assert ev_77 is not None
    assert ev_77.get('start') == '2026-07-25', (
        f'Briefing event start={ev_77.get("start")!r}, expected 2026-07-25 (departure day)')
    # Nicht Regression: FRA→YYC-Leg (Jul 24) bleibt auf Jul 24
    d24 = secs.get('2026-07-24') or []
    assert any(s.get('flight') == '4Y76' for s in d24), 'FRA-YYC regression: not on Jul 24'


def test_attach_sectors_backfills_flight_from_legs():
    """Nico-L-Fall (2026-07-23, Discover): VEVENT-Summary gibt nur die Route
    her (flight=None im Sektor), aber der Tages-Parser hat die Nummern in
    `legs` ('4Y 1224' MIT Leerzeichen). _attach_sectors muss die fehlenden
    Sektor-Flugnummern routen-gleich und Reihenfolge-stabil auffüllen —
    sonst können Feed/Live-Karte/Radar den Flug nie auflösen."""
    briefings = {'2026-07-23': {
        'legs': [
            {'flight': '4Y 1224', 'from': 'FRA', 'to': 'HER',
             'dep': '13:45', 'arr': '17:45'},
            {'flight': '4Y 1225', 'from': 'HER', 'to': 'FRA',
             'dep': '18:35', 'arr': '20:55'},
        ],
    }}
    events = [
        {'summary': 'Flugdienst', 'location': 'FRA - HER',
         'start_iso': '2026-07-23T11:45:00Z', 'end_iso': '2026-07-23T14:45:00Z'},
        {'summary': 'Flugdienst', 'location': 'HER - FRA',
         'start_iso': '2026-07-23T15:35:00Z', 'end_iso': '2026-07-23T18:55:00Z'},
    ]
    backend._attach_sectors(briefings, events)
    secs = briefings['2026-07-23']['ical_sectors']
    assert [(s['flight'], s['from'], s['to']) for s in secs] == [
        ('4Y1224', 'FRA', 'HER'), ('4Y1225', 'HER', 'FRA')]


def test_attach_sectors_backfill_never_overwrites_parsed_flight():
    """Backfill füllt NUR None-Flüge — ein aus dem Summary geparster Flug
    gewinnt immer (legs könnten stale sein)."""
    briefings = {'2026-07-23': {
        'legs': [{'flight': '4Y 9999', 'from': 'FRA', 'to': 'HER'}],
    }}
    events = [
        {'summary': '4Y 1224: FRA - HER', 'location': '',
         'start_iso': '2026-07-23T11:45:00Z', 'end_iso': '2026-07-23T14:45:00Z'},
    ]
    backend._attach_sectors(briefings, events)
    secs = briefings['2026-07-23']['ical_sectors']
    assert secs[0]['flight'] == '4Y1224'
