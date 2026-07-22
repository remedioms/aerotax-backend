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
    # Overlay + Import-Cache sauber starten (Disk-Dateien + Profil-Mirror)
    for p in (backend._logbook_overlay_path(TOKEN),
              backend._logbook_import_path(TOKEN)):
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    try:
        pf = backend._profile_load(TOKEN) or {}
        prof = (pf.get('profile') or {})
        if prof.get('logbook_overlay'):
            prof['logbook_overlay'] = {}
            backend._profile_save(TOKEN, prof)
    except Exception:
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


# ── Import-Merge (ax_logbook_import, White-Glove-Einspielung) ───────────────

IMPORT_BLOB = {'legs': [
    {'date': '2019-03-05', 'flight': 'LH500', 'from': 'MUC', 'to': 'GRU',
     'dep_iso': '2019-03-05T21:00:00+00:00',
     'arr_iso': '2019-03-06T07:30:00+00:00',
     'block_min': 630, 'reg': 'D-AIXA', 'type': 'A359', 'pf': True,
     'ldg_night': 1, 'night_min': 400, 'role': 'FO'},
    # Kollision mit Roster-Leg LH400 → Roster gewinnt, aber Landungen/PF
    # aus dem Import bleiben als Fallback erhalten
    {'date': '2026-05-01', 'flight': 'LH400', 'from': 'FRA', 'to': 'JFK',
     'block_min': 999, 'type': 'FAKE', 'ldg_day': 1, 'pf': True},
    # kaputte Blockzeit (Platzhalter) → Leg bleibt, block_min fällt raus
    {'date': '2014-02-17', 'flight': 'LH2477', 'from': 'LHR', 'to': 'MUC',
     'block_min': 1439, 'type': 'A320'},
], 'sim': [{'date': '2019-04-01', 'place': 'FRA', 'code': 'RE359',
            'duration_min': 240}]}


def _seed_import(blob=IMPORT_BLOB):
    backend._atomic_write_json(backend._logbook_import_path(TOKEN), blob)


def test_import_merge_dedupe_and_sort():
    _seed()
    _seed_import()
    r = _get()
    assert r['imported_legs'] == 2
    keys = [e['key'] for e in r['entries']]
    # chronologisch: Import-Legs (2014/2019) vor den Roster-Legs (2026)
    assert keys[0] == '2014-02-17|LH2477|LHR|MUC'
    assert keys[1] == '2019-03-05|LH500|MUC|GRU'
    # Dedupe: LH400 nur EINMAL, und zwar aus dem Roster (type 346, nicht FAKE)
    lh400 = [e for e in r['entries'] if e['flight'] == 'LH400']
    assert len(lh400) == 1 and lh400[0]['type'] == '346'
    # Überlapp-Fallback: CSV-Landungen/PF überleben die Roster-Dedupe
    assert lh400[0]['ldg_day'] == 1 and lh400[0]['pf'] is True
    assert lh400[0]['block_min'] == 520      # Blockzeit bleibt die vom Roster
    imp = {e['flight']: e for e in r['entries'] if e.get('source') == 'import'}
    assert imp['LH500']['block_min'] == 630 and imp['LH500']['pf'] is True
    assert imp['LH500']['reg'] == 'D-AIXA' and imp['LH500']['role'] == 'FO'
    # 1439-Minuten-Platzhalter wird nicht als Blockzeit übernommen
    assert imp['LH2477']['block_min'] is None
    # Sim-Sessions erscheinen NICHT als Flug-Legs
    assert not any(e['flight'] == 'SIM' for e in r['entries'])


def test_import_totals_and_by_type():
    _seed()
    _seed_import()
    r = _get()
    assert r['totals']['legs'] == 5
    assert r['totals']['block_min'] == 520 + 450 + 60 + 630
    # ldg_night des Import-Legs + ldg_day-Fallback auf dem Überlapp-Leg LH400
    assert r['totals']['landings'] == 2
    bt = {t['type']: t for t in r['by_type']}
    assert bt['A359']['legs'] == 1 and bt['A359']['block_min'] == 630


def test_overlay_wins_over_import():
    _seed()
    _seed_import()
    with backend.app.test_request_context(json={
            'date': '2019-03-05', 'flight': 'LH500', 'from': 'MUC', 'to': 'GRU',
            'ldg_night': 2, 'pf': False, 'remarks': 'korrigiert'}):
        assert backend.save_logbook_leg(TOKEN).get_json()['ok']
    r = _get()
    e = [x for x in r['entries'] if x['flight'] == 'LH500'][0]
    assert e['ldg_night'] == 2 and e['pf'] is False and e['remarks'] == 'korrigiert'


def test_no_import_store_keeps_logbook_unchanged():
    _seed()
    r = _get()
    assert r['imported_legs'] == 0 and r['totals']['legs'] == 3


def test_sim_sessions_separate_from_flight_totals():
    _seed()
    _seed_import()
    r = _get()
    # Sim-Session aus dem Import: eigene Sektion, neueste zuerst
    assert r['sim_total_min'] == 240
    assert r['sim_sessions'] == [{'date': '2019-04-01', 'place': 'FRA',
                                  'code': 'RE359', 'duration_min': 240,
                                  'role': None}]
    # FCL.050: FSTD-Zeit zählt NICHT in die Flug-Totals/Muster-Summen
    assert r['totals']['block_min'] == 520 + 450 + 60 + 630
    assert not any(t['type'] == 'RE359' for t in r['by_type'])


def test_sim_sessions_empty_without_import():
    _seed()
    r = _get()
    assert r['sim_sessions'] == [] and r['sim_total_min'] == 0
