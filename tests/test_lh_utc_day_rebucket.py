"""Audit 2026-07-31, Befund 1: LH-Flug-Legs gehören auf den UTC-Tag.

Echter Fall (157-Tage-Audit gegen sechs amtliche Monats-PDFs + LH-Raw):
LH457 LAX→FRA, dep 2026-04-26T22:30:00Z (= 15:30 Ortszeit LAX). PUB UND
LH-Rohdaten (rosterDays[].day) keyen den Flug auf den 26.04. Die App legte
den Tagestext auf den 27.04. (Berlin-Bucket: 22:30Z = 00:30 CEST Folgetag),
während _build_ical_sectors denselben Flug korrekt aufs UTC-Datum (26.) keyt —
Selbstwiderspruch UND Widerspruch zum amtlichen Plan.

Maßstab ist die AMTLICHE Zuordnung: LH keyed UTC (nicht Berlin, und auch
nicht stations-lokal wie SWISS-F1 — LH401 ab JFK ~01:55Z = 21:55 Ortszeit
des Vortags steht amtlich auf dem UTC-Tag; stations-lokal wäre dort falsch).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend


def _pipeline(ics_text):
    """Parse + LH-Rebucket wie im Import-Pfad (ohne SWISS/ITA-Stufen)."""
    evs = backend._parse_ics_to_events(ics_text)
    evs = backend._lh_rebucket_utc_flight_days(evs)
    briefings, _ = backend._ics_events_to_briefings(evs, existing={})
    backend._attach_sectors(briefings, evs)
    return evs, briefings


def _wrap(vevents):
    return 'BEGIN:VCALENDAR\r\nVERSION:2.0\r\n' + vevents + '\r\nEND:VCALENDAR'


# ── Der Audit-Fall: LH457 LAX→FRA am 26.04. ──────────────────────────────────

LH457_ICS = _wrap(
    'BEGIN:VEVENT\r\n'
    'UID:lay-lax@test\r\n'
    'SUMMARY:Layover [LAX]\r\n'
    'LOCATION:LAX\r\n'
    'DTSTART:20260424T084700Z\r\n'
    'DTEND:20260426T223000Z\r\n'
    'END:VEVENT\r\n'
    'BEGIN:VEVENT\r\n'
    'UID:lh457@test\r\n'
    'SUMMARY:LH 457: LAX-FRA\r\n'
    'LOCATION:LAX - FRA\r\n'
    'DTSTART:20260426T223000Z\r\n'
    'DTEND:20260427T084700Z\r\n'
    'END:VEVENT')


def test_lh457_lands_on_official_day_26():
    _, briefings = _pipeline(LH457_ICS)
    d26 = briefings.get('2026-04-26') or {}
    assert 'LH 457: LAX-FRA' in (d26.get('ical_summary') or ''), \
        'Der Flug muss auf dem amtlichen Tag (26.04.) stehen'
    # Der 27. darf den Flug nur noch als Übernacht-Fortsetzung tragen,
    # nicht als (einzigen) Tagestext ohne Tag-Suffix.
    d27 = briefings.get('2026-04-27') or {}
    s27 = d27.get('ical_summary') or ''
    if 'LH 457' in s27:
        assert '(Tag 2/2)' in s27


def test_lh457_leg_dep_is_lax_wall_clock_on_day_26():
    _, briefings = _pipeline(LH457_ICS)
    legs = (briefings.get('2026-04-26') or {}).get('legs') or []
    lh457 = [l for l in legs if (l.get('flight') or '').replace(' ', '') == 'LH457']
    assert lh457, 'Leg LH457 muss am 26.04. hängen'
    assert lh457[0]['dep'] == '15:30'      # 22:30Z = 15:30 PDT am 26.04.
    assert lh457[0]['from'] == 'LAX' and lh457[0]['to'] == 'FRA'


def test_lh457_sector_and_day_text_agree():
    """Der Selbstwiderspruch (Sektor 26., Text 27.) muss weg sein."""
    _, briefings = _pipeline(LH457_ICS)
    secs26 = (briefings.get('2026-04-26') or {}).get('ical_sectors') or []
    assert any((s.get('flight') or '') == 'LH457' for s in secs26)
    secs27 = (briefings.get('2026-04-27') or {}).get('ical_sectors') or []
    assert not any((s.get('flight') or '') == 'LH457' for s in secs27)


def test_lh457_block_minutes_on_day_26():
    _, briefings = _pipeline(LH457_ICS)
    assert int((briefings.get('2026-04-26') or {}).get('block_minutes') or 0) > 0
    assert int((briefings.get('2026-04-27') or {}).get('block_minutes') or 0) == 0


# ── Grenzfälle 22:00–24:00 Z und DST-Kanten ─────────────────────────────────

def test_lh455_2140z_stays_on_same_day():
    """LH455 dep 21:40Z (Audit: 'entkommt nur knapp') — Berlin 23:40 gleicher
    Tag, UTC gleicher Tag: nichts darf wandern."""
    ics = _wrap('BEGIN:VEVENT\r\nUID:lh455@test\r\n'
                'SUMMARY:LH 455: LAX-FRA\r\nLOCATION:LAX - FRA\r\n'
                'DTSTART:20260730T214000Z\r\nDTEND:20260731T075000Z\r\n'
                'END:VEVENT')
    evs, briefings = _pipeline(ics)
    assert evs[0]['start'] == '2026-07-30'
    assert 'LH 455' in ((briefings.get('2026-07-30') or {}).get('ical_summary') or '')


def test_2200z_summer_boundary_rebuckets():
    """Exakt 22:00Z im Sommer = 00:00 CEST Folgetag → muss zurück auf den
    UTC-Tag."""
    ics = _wrap('BEGIN:VEVENT\r\nUID:b1@test\r\n'
                'SUMMARY:LH 457: LAX-FRA\r\nLOCATION:LAX - FRA\r\n'
                'DTSTART:20260426T220000Z\r\nDTEND:20260427T082000Z\r\n'
                'END:VEVENT')
    evs, _ = _pipeline(ics)
    assert evs[0]['start'] == '2026-04-26'


def test_winter_dst_2330z_rebuckets():
    """Winter (CET=UTC+1): 23:30Z = 00:30 Folgetag → UTC-Tag gewinnt."""
    ics = _wrap('BEGIN:VEVENT\r\nUID:w1@test\r\n'
                'SUMMARY:LH 511: GRU-FRA\r\nLOCATION:GRU - FRA\r\n'
                'DTSTART:20260115T233000Z\r\nDTEND:20260116T093000Z\r\n'
                'END:VEVENT')
    evs, _ = _pipeline(ics)
    assert evs[0]['start'] == '2026-01-15'


def test_winter_2230z_already_same_day_untouched():
    """Winter: 22:30Z = 23:30 CET gleicher Tag — kein Divergenz-Fenster."""
    ics = _wrap('BEGIN:VEVENT\r\nUID:w2@test\r\n'
                'SUMMARY:LH 507: GRU-FRA\r\nLOCATION:GRU - FRA\r\n'
                'DTSTART:20260115T223000Z\r\nDTEND:20260116T083000Z\r\n'
                'END:VEVENT')
    evs, _ = _pipeline(ics)
    assert evs[0]['start'] == '2026-01-15'


def test_dst_edge_gru_2123z_march_31_untouched():
    """Audit-Randfall: GRU 31.03. 21:23Z (CEST 23:23 gleicher Tag)."""
    ics = _wrap('BEGIN:VEVENT\r\nUID:gru@test\r\n'
                'SUMMARY:LH 507: GRU-FRA\r\nLOCATION:GRU - FRA\r\n'
                'DTSTART:20260331T212300Z\r\nDTEND:20260401T073000Z\r\n'
                'END:VEVENT')
    evs, _ = _pipeline(ics)
    assert evs[0]['start'] == '2026-03-31'


# ── Was NICHT wandern darf ──────────────────────────────────────────────────

def test_tzid_events_stay_station_local():
    """PDF-Pfad-Form (DTSTART;TZID=Abflugstation) ist BEWUSST stations-lokal
    gebuckert — kein _utc_z_start-Flag, kein Rebucket. PUJ 20:30 lokal =
    00:30Z Folgetag: Bucket bleibt der lokale Tag."""
    ics = _wrap('BEGIN:VEVENT\r\nUID:tz@test\r\n'
                'SUMMARY:4Y 1523: PUJ-FRA\r\nLOCATION:PUJ - FRA\r\n'
                'DTSTART;TZID=America/Santo_Domingo:20260426T203000\r\n'
                'DTEND:20260427T103000Z\r\nEND:VEVENT')
    evs, _ = _pipeline(ics)
    assert evs[0]['start'] == '2026-04-26'
    assert not evs[0].get('_utc_z_start')


def test_swiss_space_form_not_touched_by_lh_rebucket():
    """SWISS-Form (space-separiert) gehört dem F1-Fix — die LH-Colon-Regex
    darf sie nicht matchen."""
    assert not backend._LH_COLON_LEG_RE.match('LX92 ZRH 2215 GRU 0530 333')
    assert backend._LH_COLON_LEG_RE.match('LH 457: LAX-FRA')
    assert backend._LH_COLON_LEG_RE.match('DH LH 1623: KRK-MUC')
    assert backend._LH_COLON_LEG_RE.match('LH457: LAX-FRA')


def test_non_flight_evening_event_stays_berlin_bucketed():
    """Boden-/Off-Events ohne Flug bleiben Berlin-gebuckert (nur Flug-Legs
    tragen die amtliche UTC-Zuordnung; ein Abend-Termin 22:30Z OHNE Flug ist
    aus Nutzersicht ein Termin am Folgetag 00:30 lokal)."""
    ics = _wrap('BEGIN:VEVENT\r\nUID:g1@test\r\n'
                'SUMMARY:Off Day (LMN_DM1)\r\nLOCATION:FRA\r\n'
                'DTSTART:20260426T223000Z\r\nDTEND:20260426T233000Z\r\n'
                'END:VEVENT')
    evs, _ = _pipeline(ics)
    assert evs[0]['start'] == '2026-04-27'


# ── Vorlauf-Marker (Pickup/Briefing) folgen ihrem Flug ──────────────────────

def test_wrap_pulled_pickup_follows_its_flight():
    """lh_flightops zieht den Pickup-DTSTART bei Berlin-Tag-Wrap auf den
    Abflug-Zeitpunkt — der Marker muss dann mit dem Flug auf dem UTC-Tag
    stehen, nicht allein auf dem Folgetag."""
    ics = _wrap('BEGIN:VEVENT\r\nUID:pu@test\r\n'
                'SUMMARY:13:30 LT Pickup LAX\r\n'
                'DTSTART:20260426T223000Z\r\nDTEND:20260426T223000Z\r\n'
                'END:VEVENT\r\n'
                'BEGIN:VEVENT\r\nUID:f@test\r\n'
                'SUMMARY:LH 457: LAX-FRA\r\nLOCATION:LAX - FRA\r\n'
                'DTSTART:20260426T223000Z\r\nDTEND:20260427T084700Z\r\n'
                'END:VEVENT')
    evs, briefings = _pipeline(ics)
    pu = [e for e in evs if 'Pickup' in (e.get('summary') or '')][0]
    assert pu['start'] == '2026-04-26'
    assert 'Pickup' in ((briefings.get('2026-04-26') or {}).get('ical_summary') or '')


def test_in_window_briefing_follows_flight():
    """Briefing 22:15Z vor Abflug 23:30Z: beide auf dem UTC-Tag."""
    ics = _wrap('BEGIN:VEVENT\r\nUID:br@test\r\n'
                'SUMMARY:15:15 LT Briefing LAX\r\n'
                'DTSTART:20260426T221500Z\r\nDTEND:20260426T221500Z\r\n'
                'END:VEVENT\r\n'
                'BEGIN:VEVENT\r\nUID:f2@test\r\n'
                'SUMMARY:LH 457: LAX-FRA\r\nLOCATION:LAX - FRA\r\n'
                'DTSTART:20260426T233000Z\r\nDTEND:20260427T094700Z\r\n'
                'END:VEVENT')
    evs, _ = _pipeline(ics)
    br = [e for e in evs if 'Briefing' in (e.get('summary') or '')][0]
    assert br['start'] == '2026-04-26'


def test_homebase_early_briefing_follows_next_day_flight():
    """Gegenrichtung: Briefing 00:30 CEST (= 22:30Z Vortag) vor einem
    00:30Z-Abflug am Folge-UTC-Tag → Marker gehört zum Flug-Tag (Folgetag),
    exakt wie der bisherige Berlin-Bucket es schon richtig machte."""
    ics = _wrap('BEGIN:VEVENT\r\nUID:br2@test\r\n'
                'SUMMARY:00:30 LT Briefing FRA\r\n'
                'DTSTART:20260426T223000Z\r\nDTEND:20260426T223000Z\r\n'
                'END:VEVENT\r\n'
                'BEGIN:VEVENT\r\nUID:f3@test\r\n'
                'SUMMARY:LH 606: FRA-JED\r\nLOCATION:FRA - JED\r\n'
                'DTSTART:20260427T003000Z\r\nDTEND:20260427T060000Z\r\n'
                'END:VEVENT')
    evs, _ = _pipeline(ics)
    br = [e for e in evs if 'Briefing' in (e.get('summary') or '')][0]
    assert br['start'] == '2026-04-27'


def test_orphan_marker_without_flight_keeps_berlin_bucket():
    """Kein folgender Flug ≤6 h → keine geratene Zuordnung, Berlin bleibt."""
    ics = _wrap('BEGIN:VEVENT\r\nUID:pu2@test\r\n'
                'SUMMARY:13:30 LT Pickup LAX\r\n'
                'DTSTART:20260426T223000Z\r\nDTEND:20260426T223000Z\r\n'
                'END:VEVENT\r\n'
                'BEGIN:VEVENT\r\nUID:f4@test\r\n'
                'SUMMARY:LH 400: FRA-JFK\r\nLOCATION:FRA - JFK\r\n'
                'DTSTART:20260428T100000Z\r\nDTEND:20260428T183000Z\r\n'
                'END:VEVENT')
    evs, _ = _pipeline(ics)
    pu = [e for e in evs if 'Pickup' in (e.get('summary') or '')][0]
    assert pu['start'] == '2026-04-27'


# ── JFK-Gegenprobe: stations-lokal wäre für LH FALSCH ───────────────────────

def test_lh401_jfk_0155z_stays_on_utc_day_not_local_day():
    """LH401 dep JFK 01:55Z am 22.03. (= 21:55 Ortszeit am 21.03.). Amtlich
    steht er auf dem UTC-Tag (22.). Berlin (02:55/03:55 lokal) = 22. — hier
    stimmen UTC und Berlin überein; ein stations-lokaler F1-Klon hätte ihn
    fälschlich auf den 21. gezogen."""
    ics = _wrap('BEGIN:VEVENT\r\nUID:lh401@test\r\n'
                'SUMMARY:LH 401: JFK-FRA\r\nLOCATION:JFK - FRA\r\n'
                'DTSTART:20260322T015500Z\r\nDTEND:20260322T085500Z\r\n'
                'END:VEVENT')
    evs, briefings = _pipeline(ics)
    assert evs[0]['start'] == '2026-03-22'
    assert 'LH 401' in ((briefings.get('2026-03-22') or {}).get('ical_summary') or '')


def test_rebucket_is_idempotent():
    evs = backend._parse_ics_to_events(LH457_ICS)
    evs = backend._lh_rebucket_utc_flight_days(evs)
    snap = [(e.get('start'), e.get('end')) for e in evs]
    evs = backend._lh_rebucket_utc_flight_days(evs)
    assert snap == [(e.get('start'), e.get('end')) for e in evs]
