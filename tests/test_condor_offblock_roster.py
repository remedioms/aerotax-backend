"""Condor (cube.aero) + offblock.de — Format-Normalisierung + Layover-Synthese.

Echte-User-Audit 2026-07-21 (114 Condor-Feeds, 5 offblock-Feeds live):
  Condor:   „C/I" (Check-in, LOCATION=IATA), „P/U" (Pickup, LOCATION=IATA),
            Flüge „DE2080 FRA-LAX", Codes ORT/U/-/S_OFF; KEINE LAYOVER-Events.
  offblock: „VL1144: FRA ✈ BIO" (✈ statt Bindestrich), teils ICAO-Stationen
            („EW 7276: EDDH ✈ LOWS") — vorher NIE ein Sektor erkannt.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as backend  # noqa: E402


def _ev(summary, start, end, location=''):
    ev = {'summary': summary, 'location': location,
          'start_iso': start, 'end_iso': end,
          'start': start[:10], 'end': end[:10],
          '_is_date_only_start': False, '_is_date_only_end': False}
    ev['_multiday_dates'] = backend._ics_multiday_dates(ev)
    return ev


def _condor_trip():
    # Echte Muster (JFK-Rotation, UTC-Zeiten wie cube.aero sie liefert).
    return [
        _ev('C/I', '2026-07-03T07:50:00Z', '2026-07-03T08:40:00Z', 'FRA'),
        _ev('DE2016 FRA-JFK', '2026-07-03T10:40:00Z', '2026-07-03T19:05:00Z', 'FRA - JFK'),
        _ev('P/U', '2026-07-04T17:00:00Z', '2026-07-04T17:00:00Z', 'JFK'),
        _ev('DE2017 JFK-FRA', '2026-07-04T20:55:00Z', '2026-07-05T04:20:00Z', 'JFK - FRA'),
        _ev('ORT', '2026-07-05T22:00:00Z', '2026-07-06T22:00:00Z', 'FRA'),
    ]


def _pipeline(events):
    events = backend._normalize_thirdparty_roster_events(events)
    events = backend._swissify_roster_events(events)
    events = backend._itaify_roster_events(events)
    events = backend._generic_layover_synthesis(events)
    briefings, _ = backend._ics_events_to_briefings(events)
    backend._attach_sectors(briefings, events)
    return events, briefings


def test_condor_checkin_becomes_briefing_token():
    _, briefings = _pipeline(_condor_trip())
    # C/I 07:50Z an FRA = 09:50 Ortszeit → LH-Token.
    assert '09:50 LT Briefing FRA' in (briefings['2026-07-03'].get('ical_summary') or '')


def test_condor_pickup_becomes_pickup_token():
    _, briefings = _pipeline(_condor_trip())
    # P/U 17:00Z am JFK = 13:00 Ortszeit.
    assert 'Pickup 13:00' in (briefings['2026-07-04'].get('ical_summary') or '')


def test_condor_layover_synthesized():
    events, briefings = _pipeline(_condor_trip())
    lays = [e for e in events if e.get('summary') == 'LAYOVER']
    assert [l['location'] for l in lays] == ['JFK']
    assert briefings['2026-07-03'].get('ical_layover_ort') == 'JFK'
    assert briefings['2026-07-04'].get('ical_layover_ort') == 'JFK'


def test_condor_sectors_and_block():
    _, briefings = _pipeline(_condor_trip())
    secs = briefings['2026-07-03'].get('ical_sectors') or []
    assert [(s['flight'], s['from'], s['to']) for s in secs] == [('DE2016', 'FRA', 'JFK')]
    assert briefings['2026-07-03'].get('block_minutes') == 505


def test_offblock_plane_glyph_and_icao_normalized():
    evs = [
        _ev('VL1144: FRA ✈ BIO', '2026-06-18T13:29:00Z', '2026-06-18T15:35:00Z'),
        _ev('Briefing: VL1144', '2026-06-18T11:45:00Z', '2026-06-18T12:30:00Z'),
        _ev('EW 7276: EDDH ✈ LOWS', '2026-06-30T09:22:00Z', '2026-06-30T10:35:00Z'),
    ]
    events, briefings = _pipeline(evs)
    assert events[0]['summary'] == 'VL1144: FRA - BIO'
    assert events[2]['summary'] == 'EW 7276: HAM - SZG'
    secs18 = briefings['2026-06-18'].get('ical_sectors') or []
    assert [(s['flight'], s['from'], s['to']) for s in secs18] == [('VL1144', 'FRA', 'BIO')]
    secs30 = briefings['2026-06-30'].get('ical_sectors') or []
    assert [(s['from'], s['to']) for s in secs30] == [('HAM', 'SZG')]


def test_normalizer_noop_for_lh_swiss():
    lh = _ev('LH 390: FRA-LUX', '2026-07-22T08:00:00Z', '2026-07-22T09:00:00Z', 'FRA')
    sw = _ev('LX1270 ZRH 1236 CPH 1413 32B', '2026-07-22T10:36:00Z', '2026-07-22T12:13:00Z')
    before = [dict(lh), dict(sw)]
    after = backend._normalize_thirdparty_roster_events([lh, sw])
    assert after == before


def test_generic_synthesis_skips_lh_and_edelweiss():
    # LH: echtes LAYOVER-Event vorhanden → kein Doppeln.
    lh = [
        _ev('LH 400: FRA-JFK', '2026-07-03T10:40:00Z', '2026-07-03T19:05:00Z'),
        _ev('LAYOVER', '2026-07-03T19:05:00Z', '2026-07-04T20:55:00Z', 'JFK'),
        _ev('LH 401: JFK-FRA', '2026-07-04T20:55:00Z', '2026-07-05T04:20:00Z'),
    ]
    n_before = len(lh)
    assert len(backend._generic_layover_synthesis(lh)) == n_before
    # Edelweiss-Outlook: „LAY"-Signal → eigener (verifizierter) Pfad, no-op.
    wk = [
        _ev('CC9 (WK36 ZRH-SJO)', '2026-06-19T06:40:00Z', '2026-06-19T18:20:00Z'),
        _ev('LAY', '2026-06-19T22:26:00Z', '2026-06-21T18:00:00Z'),
        _ev('CC9 (WK38 SJO-LIR) | CC9 (WK38 LIR-ZRH)', '2026-06-21T19:20:00Z', '2026-06-22T07:40:00Z'),
    ]
    assert len(backend._generic_layover_synthesis(wk)) == 3


def test_pdf_import_protected_from_ek_reconcile(monkeypatch, tmp_path):
    """Discover/City-PDF (source='pdf', url='') gilt 35 Tage als frischer Feed —
    der EKEvent-Push darf die PDF-Tage nicht mehr wegräumen (Echte-User-Befund
    2026-07-22: 33 PDF-Events → 4 Tage nach EK-Sync)."""
    from datetime import datetime, timedelta
    import json as _json
    tok = 'AT-TEST-PDFGUARD-000000'
    profile = {'profile': {'calendar_feed': {
        'url': '', 'source': 'pdf',
        'imported_at': (datetime.now() - timedelta(days=3)).isoformat(),
        'events': []}}}
    monkeypatch.setattr(backend, '_validate_token', lambda t: backend._TokenValidationResult(backend._TokenValidationState.VALID))
    monkeypatch.setattr(backend, '_profile_load', lambda t: profile)
    monkeypatch.setattr(backend, '_profile_save', lambda *a, **k: True)
    monkeypatch.setattr(backend, '_ical_briefings_load', lambda t: {})
    monkeypatch.setattr(backend, '_ical_briefings_save', lambda t, b: True)
    called = {}
    def _spy_reconcile(*a, **k):
        called['reconcile'] = True
        return {'feed_dates': 0, 'cleared': 0, 'window': None}
    monkeypatch.setattr(backend, '_reconcile_month_briefings', _spy_reconcile)
    monkeypatch.setattr(backend, '_user_profile_path',
                        lambda t: str(tmp_path / 'p.json'))
    c = backend.app.test_client()
    r = c.post(f'/api/user/calendar-events/{tok}/upload',
               headers={'Authorization': f'Bearer {tok}'},
               json={'events': [{'summary': 'OFF',
                                 'start_iso': '2026-07-25T00:00:00Z',
                                 'end_iso': '2026-07-25T23:00:00Z'}]})
    body = r.get_json() or {}
    assert (body.get('reconcile') or {}).get('skipped') == 'url_feed_fresher', body
    assert 'reconcile' not in called
