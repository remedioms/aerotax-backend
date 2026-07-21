"""Cockpit-Flugbuch (FCL.050-Stil): per-Leg Blockzeit + Reg/Muster aus den
Roster-Sektoren + manuelles Overlay (Landungen/PF/Nacht) + Summen pro Muster.
Rein offline — seedet Sektoren über den manual-briefings-Store, kein Netz."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend

TOKEN = 'AT-TEST-LOGBOOK-UNIT'

DAYS = {
    '2026-05-01': {'ical_sectors': [
        {'flight': 'LH400', 'from': 'FRA', 'to': 'JFK',
         'dep_iso': '2026-05-01T10:55:00+02:00',
         'arr_iso': '2026-05-01T13:35:00-04:00',
         'tail': 'D-AIHY', 'type': '346'}]},
    '2026-05-03': {'ical_sectors': [
        {'flight': 'LH401', 'from': 'JFK', 'to': 'FRA',
         'dep_iso': '2026-05-03T18:00:00-04:00',
         'arr_iso': '2026-05-04T07:30:00+02:00',   # Übernacht
         'reg': 'D-AIHY', 'type': '346'},
        {'flight': 'LH222', 'from': 'FRA', 'to': 'MUC',
         'dep_iso': '2026-05-04T09:00:00+02:00',
         'arr_iso': '2026-05-04T10:00:00+02:00',
         'type': '32N'}]},
}


def _seed():
    backend._manual_briefings_save(TOKEN, DAYS)
    # Overlay sauber starten
    try:
        p = backend._logbook_overlay_path(TOKEN)
        if p and os.path.exists(p):
            os.remove(p)
    except OSError:
        pass


def _get():
    with backend.app.test_request_context():
        rv = backend.get_logbook(TOKEN)
    return (rv.get_json() if hasattr(rv, 'get_json') else rv[0].get_json())


def test_block_min_from_offset_iso():
    assert backend._logbook_block_min(
        '2026-05-01T10:55:00+02:00', '2026-05-01T13:35:00-04:00') == 520
    # Übernacht korrekt (Offset macht es eindeutig)
    assert backend._logbook_block_min(
        '2026-05-03T18:00:00-04:00', '2026-05-04T07:30:00+02:00') == 450
    # unplausibel → None
    assert backend._logbook_block_min('x', 'y') is None
    assert backend._logbook_block_min(
        '2026-05-01T10:00:00+02:00', '2026-05-01T09:00:00+02:00') is None  # negativ


def test_logbook_entries_and_totals():
    _seed()
    r = _get()
    assert r['ok'] is True
    assert r['totals']['legs'] == 3
    assert r['totals']['days'] == 2
    assert r['totals']['block_min'] == 520 + 450 + 60
    e = {x['flight']: x for x in r['entries']}
    assert e['LH400']['block_min'] == 520
    assert e['LH400']['reg'] == 'D-AIHY' and e['LH400']['type'] == '346'
    assert e['LH222']['block_min'] == 60


def test_by_type_aggregation():
    _seed()
    r = _get()
    bt = {t['type']: t for t in r['by_type']}
    assert bt['346']['legs'] == 2 and bt['346']['block_min'] == 970
    assert bt['32N']['legs'] == 1 and bt['32N']['block_min'] == 60
    # nach block_min absteigend sortiert
    assert r['by_type'][0]['type'] == '346'


def test_save_and_readback_overlay():
    _seed()

    def _save(body):
        with backend.app.test_request_context(json=body):
            return backend.save_logbook_leg(TOKEN).get_json()

    s = _save({'date': '2026-05-01', 'flight': 'LH400', 'from': 'FRA',
               'to': 'JFK', 'ldg_day': 1, 'pf': True, 'remarks': 'PF Langstrecke'})
    assert s['ok'] and s['overlay']['ldg_day'] == 1 and s['overlay']['pf'] is True
    # zweites Leg: Nachtlandung
    _save({'date': '2026-05-03', 'flight': 'LH401', 'from': 'JFK', 'to': 'FRA',
           'ldg_night': 1, 'night_min': 300})
    r = _get()
    e = {x['flight']: x for x in r['entries']}
    assert e['LH400']['ldg_day'] == 1 and e['LH400']['pf'] is True
    assert e['LH400']['remarks'] == 'PF Langstrecke'
    assert e['LH401']['ldg_night'] == 1 and e['LH401']['night_min'] == 300
    # Landungs-Summe reflektiert das Overlay
    assert r['totals']['landings'] == 2


def test_save_rejects_incomplete_leg():
    with backend.app.test_request_context(json={'date': '2026-05-01', 'flight': 'LH400'}):
        rv = backend.save_logbook_leg(TOKEN)
    resp, status = (rv if isinstance(rv, tuple) else (rv, 200))
    assert status == 400 and resp.get_json()['error'] == 'leg_incomplete'


def test_clearing_overlay_removes_entry():
    _seed()

    def _save(body):
        with backend.app.test_request_context(json=body):
            return backend.save_logbook_leg(TOKEN).get_json()
    _save({'date': '2026-05-01', 'flight': 'LH400', 'from': 'FRA',
           'to': 'JFK', 'ldg_day': 2})
    # jetzt zurücksetzen (alle Werte leer)
    out = _save({'date': '2026-05-01', 'flight': 'LH400', 'from': 'FRA',
                 'to': 'JFK', 'ldg_day': None, 'pf': False})
    # pf False ist ein echter Wert → bleibt; ldg_day None raus
    assert 'ldg_day' not in out['overlay']
