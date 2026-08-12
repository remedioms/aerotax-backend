"""Cockpit-Flugbuch (FCL.050-Stil): per-Leg Blockzeit + Reg/Muster aus den
Roster-Sektoren + manuelles Overlay (Landungen/PF/Nacht) + Summen pro Muster.
Rein offline — seedet Sektoren über den manual-briefings-Store, kein Netz."""
import os
import sys
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend

TOKEN = 'AT-TEST-LOGBOOK-UNIT'

DAYS = {
    '2026-05-01': {'ical_sectors': [
        {'flight': 'LH400', 'from': 'FRA', 'to': 'JFK',
         'dep_iso': '2026-05-01T10:55:00+02:00',
         'arr_iso': '2026-05-01T13:35:00-04:00',
         'est_arr_iso': '2026-05-01T13:35:00-04:00',
         'arr_measured': True,
         'tail': 'D-AIHY', 'type': '346'}]},
    '2026-05-03': {'ical_sectors': [
        {'flight': 'LH401', 'from': 'JFK', 'to': 'FRA',
         'dep_iso': '2026-05-03T18:00:00-04:00',
         'arr_iso': '2026-05-04T07:30:00+02:00',   # Übernacht
         'est_arr_iso': '2026-05-04T07:30:00+02:00',
         'arr_measured': True,
         'reg': 'D-AIHY', 'type': '346'},
        {'flight': 'LH222', 'from': 'FRA', 'to': 'MUC',
         'dep_iso': '2026-05-04T09:00:00+02:00',
         'arr_iso': '2026-05-04T10:00:00+02:00',
         'est_arr_iso': '2026-05-04T10:00:00+02:00',
         'arr_measured': True,
         'type': '32N'}]},
}


def _seed():
    backend._manual_briefings_save(TOKEN, DAYS)
    # Overlay + Import- + Anreicherungs-Cache sauber starten (Disk-Dateien +
    # Profil-Mirror). Der facts-Cache ist seit 2026-07-27 die Quelle für
    # nachgezogene Reg/Typ — ein Rest aus einem Vorlauf würde hier einstreuen.
    for p in (backend._logbook_overlay_path(TOKEN),
              backend._logbook_import_path(TOKEN),
              backend._logbook_facts_path(TOKEN)):
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


def test_roster_leg_needs_real_arrival_proof_and_never_accepts_plan_only():
    now = datetime(2026, 8, 3, 18, 58, tzinfo=timezone.utc)
    plan_only = {'arr_iso': '2026-08-03T18:45:00Z'}
    measured_past = {'arr_iso': '2026-08-03T18:45:00Z',
                     'est_arr_iso': '2026-08-03T18:43:00Z',
                     'arr_measured': True}
    measured_future = {'arr_iso': '2026-08-04T00:05:00Z',
                       'est_arr_iso': '2026-08-04T00:09:00Z',
                       'arr_measured': True}
    cancelled = dict(measured_past, cancelled=True)
    cancelled_text = dict(measured_past, status='Cancelled')
    prediction_only = {
        'arr_iso': '2026-08-03T18:45:00Z',
        'est_arr_iso': '2026-08-03T18:50:00Z',
        'arr_measured': False,
    }

    assert backend._logbook_roster_leg_completed(plan_only, now=now) is False
    assert backend._logbook_roster_leg_completed(measured_past, now=now) is True
    assert backend._logbook_roster_leg_completed(measured_future, now=now) is False
    assert backend._logbook_roster_leg_completed(cancelled, now=now) is False
    assert backend._logbook_roster_leg_completed(cancelled_text, now=now) is False
    assert backend._logbook_roster_leg_completed(
        prediction_only, now=now) is False
    assert backend._logbook_roster_leg_completed(
        plan_only, now=now,
        proof={'actual_arr_iso': '2026-08-03T18:44:00Z'}) is True
    assert backend._logbook_roster_leg_completed(
        plan_only, now=now,
        proof={'explicit_user_entry': True}) is True
    assert backend._logbook_roster_leg_completed(
        cancelled, now=now,
        proof={'explicit_user_entry': True}) is True
    assert backend._logbook_roster_leg_completed(
        cancelled, now=now,
        proof={'historical_import': True}) is True


def test_roster_history_beyond_evidence_window_needs_no_proof():
    """Regression Paula/Florian 2026-08-05 (aus d05e779): Für Legs jenseits
    des Beweis-Fensters gibt es prinzipbedingt nie einen Beleg (der Backfill
    fasst ältere Tage nicht an) — der konservierte Roster IST dort die
    Historie. Ohne die Altersregel verschwanden ganze Monate aus
    Flugbuch+Passport (Florian: Mai/Juni komplett, Juli teilweise; Paula:
    „nur noch August"). Fenster = _LOGBOOK_EVIDENCE_WINDOW_DAYS (7,
    Owner-Entscheid 2026-08-05)."""
    assert backend._LOGBOOK_EVIDENCE_WINDOW_DAYS == 7
    now = datetime(2026, 8, 5, 8, 0, tzinfo=timezone.utc)
    # Florians echtes Mai-Leg (LH1800 MUC-MAD, Alt-Format: nur Plan-Felder).
    old_plan = {'arr_iso': '2026-05-03T09:08:00Z'}
    old_dep_only = {'dep_iso': '2026-05-03T06:29:00Z'}
    old_cancelled = {'arr_iso': '2026-05-03T09:08:00Z', 'cancelled': True}
    old_cancelled_text = {'arr_iso': '2026-05-03T09:08:00Z',
                          'status': 'Cancelled'}
    # Im Fenster (< 7 Tage) bleibt das Gate streng beweisbasiert.
    recent_plan = {'arr_iso': '2026-08-03T09:00:00Z'}
    boundary_out = {'arr_iso': '2026-07-29T07:59:00Z'}   # now-7d = 07-29 08:00
    boundary_in = {'arr_iso': '2026-07-29T08:01:00Z'}

    assert backend._logbook_roster_leg_completed(old_plan, now=now) is True
    assert backend._logbook_roster_leg_completed(old_dep_only, now=now) is True
    assert backend._logbook_roster_leg_completed(old_cancelled, now=now) is False
    assert backend._logbook_roster_leg_completed(
        old_cancelled_text, now=now) is False
    assert backend._logbook_roster_leg_completed(recent_plan, now=now) is False
    assert backend._logbook_roster_leg_completed(boundary_out, now=now) is True
    assert backend._logbook_roster_leg_completed(boundary_in, now=now) is False


def test_measured_arrival_from_facts_rejects_frozen_forecast_and_status_only():
    good = {
        'est_arr': '2026-08-03T18:45:00Z', 'arr_status': 'landed',
        'arr_esti_changed_at': '2026-08-03T18:45:30Z',
    }
    frozen = dict(good, arr_esti_changed_at='2026-08-03T17:00:00Z')
    no_terminal = dict(good, arr_status='expected')
    no_stamp = {'est_arr': good['est_arr'], 'arr_status': 'landed'}
    assert backend._logbook_measured_arrival_from_facts(good) == \
        '2026-08-03T18:45:00Z'
    assert backend._logbook_measured_arrival_from_facts(frozen) is None
    assert backend._logbook_measured_arrival_from_facts(no_terminal) is None
    assert backend._logbook_measured_arrival_from_facts(no_stamp) is None


def test_completed_only_merge_hides_future_roster_but_keeps_import(monkeypatch):
    days = {'2026-08-03': {'ical_sectors': [
        {'flight': 'LH2129', 'from': 'DRS', 'to': 'MUC',
         'dep_iso': '2026-08-03T17:50:00Z',
         'arr_iso': '2026-08-03T18:45:00Z',
         'est_arr_iso': '2026-08-03T18:44:00Z', 'arr_measured': True},
        {'flight': 'LH586', 'from': 'MUC', 'to': 'CAI',
         'dep_iso': '2026-08-03T20:05:00Z',
         'arr_iso': '2026-08-04T00:05:00Z',
         'est_arr_iso': '2026-08-04T00:09:00Z', 'arr_measured': True},
    ]}}
    imported = {'legs': [{
        'date': '2027-01-01', 'flight': 'OLD1', 'from': 'FRA', 'to': 'MUC',
        'dep_iso': '2027-01-01T10:00:00Z',
        'arr_iso': '2027-01-01T11:00:00Z', 'block_min': 60,
    }]}
    monkeypatch.setattr(backend, '_manual_briefings_load', lambda _t: days)
    monkeypatch.setattr(backend, '_ical_briefings_load', lambda _t: {})
    monkeypatch.setattr(backend, '_logbook_import_load', lambda _t: imported)
    now = datetime(2026, 8, 3, 18, 58, tzinfo=timezone.utc)

    legs = backend._logbook_merged_legs(
        TOKEN, completed_roster_only=True, now=now)

    assert [x['flight'] for x in legs] == ['LH2129', 'OLD1']


def test_get_logbook_never_materializes_plan_only_month_rows(monkeypatch):
    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    past = (now - timedelta(minutes=10)).isoformat().replace('+00:00', 'Z')
    future = (now + timedelta(hours=2)).isoformat().replace('+00:00', 'Z')
    days = {day: {'ical_sectors': [
        {'flight': 'LH1', 'from': 'FRA', 'to': 'MUC',
         'dep_iso': (now - timedelta(hours=2)).isoformat(),
         'arr_iso': past},                         # nur Plan → unsichtbar
        {'flight': 'LH2', 'from': 'MUC', 'to': 'FRA',
         'dep_iso': (now - timedelta(hours=1)).isoformat(),
         'arr_iso': past, 'est_arr_iso': past, 'arr_measured': True},
        {'flight': 'LH3', 'from': 'FRA', 'to': 'CAI',
         'dep_iso': now.isoformat(), 'arr_iso': future,
         'est_arr_iso': future, 'arr_measured': True},
    ]}}
    queued = []
    monkeypatch.setattr(backend, '_manual_briefings_load', lambda _t: days)
    monkeypatch.setattr(backend, '_ical_briefings_load', lambda _t: {})
    monkeypatch.setattr(backend, '_logbook_import_load', lambda _t: {})
    monkeypatch.setattr(backend, '_logbook_overlay_load', lambda _t: {})
    monkeypatch.setattr(backend, '_logbook_facts_load', lambda _t: {})
    monkeypatch.setattr(backend, '_logbook_enrich_async',
                        lambda _t, wanted: queued.extend(wanted))

    with backend.app.test_request_context():
        response = backend.get_logbook(TOKEN).get_json()

    assert [e['flight'] for e in response['entries']] == ['LH2']
    assert 'LH1' in [w[1] for w in queued]  # Beleg wird nur im Hintergrund gesucht.
    assert 'LH3' not in [w[1] for w in queued]  # Zukunft kostet keinen API-Call.


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


def test_discover_a330_variants_share_one_group_and_landing_implies_pf():
    _seed()
    discover = {'legs': [
        {'date': '2023-01-01', 'flight': '4Y1', 'from': 'FRA', 'to': 'CUN',
         'block_min': 600, 'type': '333', 'ldg_day': 1},
        {'date': '2023-01-03', 'flight': '4Y2', 'from': 'CUN', 'to': 'FRA',
         'block_min': 570, 'type': '33Y', 'ldg_night': 1, 'pf': False},
        {'date': '2023-01-05', 'flight': '4Y3', 'from': 'FRA', 'to': 'PHL',
         'block_min': 510, 'type': '332'},
    ]}
    _seed_import(discover)

    r = _get()

    imported = [x for x in r['entries'] if x.get('source') == 'import']
    assert [x['type'] for x in imported] == ['333', '33Y', '332']
    by_type = {x['type']: x for x in r['by_type']}
    assert by_type['A330'] == {
        'type': 'A330', 'legs': 3, 'block_min': 1680, 'landings': 2,
    }
    entries = {x['flight']: x for x in imported}
    assert entries['4Y1']['pf'] is True       # aus eigener Landung abgeleitet
    assert entries['4Y2']['pf'] is False      # explizites PM bleibt erhalten
    assert entries['4Y3']['pf'] is None       # ohne Landung nichts erfinden


def test_api_confirmed_own_landing_implies_pf_without_landing_overlay(
        monkeypatch):
    """Die personengebundene LH-API-Landung ist selbst der PF-Beleg.

    Der frühere App-Pfad wartete auf ldg_day/ldg_night im Overlay. Dadurch
    zeigte die API-Zeile bereits die LH-Landung, im Flugbuch aber noch kein PF.
    Explizites PM muss trotzdem autoritativ bleiben.
    """
    _seed()
    import blueprints.lh_flightops as fo
    monkeypatch.setattr(
        fo, 'logbook_cached_self_landing_flags',
        lambda token, candidates: {
            ('LH400', '2026-05-01', 'FRA'): True,
            ('LH401', '2026-05-03', 'JFK'): True,
        })

    # LH401 wurde ausdrücklich als PM gespeichert: API-Beleg darf diesen
    # Userwert nicht überschreiben.
    key = backend._logbook_leg_key('2026-05-03', 'LH401', 'JFK', 'FRA')
    monkeypatch.setattr(backend, '_logbook_overlay_load', lambda token: {
        key: {'pf': False},
    })

    entries = {x['flight']: x for x in _get()['entries']}
    assert entries['LH400']['pf'] is True
    assert entries['LH401']['pf'] is False
    assert entries['LH222']['pf'] is None


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
     'ldg_night': 1, 'night_min': 400, 'role': 'FO',
     'pic_name': 'MUSTERMANN, M.'},
    # Kollision mit Roster-Leg LH400 → Roster gewinnt, aber Landungen/PF
    # aus dem Import bleiben als Fallback erhalten
    {'date': '2026-05-01', 'flight': 'LH400', 'from': 'FRA', 'to': 'JFK',
     'block_min': 999, 'type': 'FAKE', 'ldg_day': 1, 'pf': True},
    # kaputte Blockzeit (Platzhalter) → Leg bleibt, block_min fällt raus
    {'date': '2014-02-17', 'flight': 'LH2477', 'from': 'LHR', 'to': 'MUC',
     'block_min': 1439, 'type': 'A320'},
], 'sim': [{'date': '2019-04-01', 'place': 'FRA', 'code': 'RE359',
            'duration_min': 240, 'instructor': 'HOHL, J.'}],
   'meta': {'carryover_min': 7800}}


def _seed_import(blob=IMPORT_BLOB):
    backend._atomic_write_json(backend._logbook_import_path(TOKEN), blob)


def test_stale_negative_import_cache_refreshes_after_direct_upsert(
        monkeypatch, tmp_path):
    path = tmp_path / 'logbook_import.json'
    backend._atomic_write_json(str(path), {})
    stale = datetime.now().timestamp() \
        - backend._LOGBOOK_IMPORT_NEGATIVE_CACHE_S - 1
    os.utime(path, (stale, stale))
    imported = {'legs': [IMPORT_BLOB['legs'][0]], 'sim': []}

    class Query:
        def select(self, *_args, **_kwargs): return self
        def eq(self, *_args, **_kwargs): return self
        def limit(self, *_args, **_kwargs): return self
        def execute(self): return SimpleNamespace(data=[imported])

    class Store:
        def table(self, name):
            assert name == 'ax_logbook_import'
            return Query()

    monkeypatch.setattr(backend, '_logbook_import_path', lambda _token: str(path))
    monkeypatch.setattr(backend, 'SB_AVAILABLE', True)
    monkeypatch.setattr(backend, 'sb', Store())
    monkeypatch.setattr(
        backend, '_supabase_execute_with_timeout',
        lambda _name, call, timeout_s: (call(), False),
    )

    assert backend._logbook_import_load(TOKEN)['legs'][0]['flight'] == 'LH500'
    assert backend._LOGBOOK_IMPORT_CACHE_S \
        > backend._LOGBOOK_IMPORT_NEGATIVE_CACHE_S


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
                                  'role': None, 'instructor': 'HOHL, J.'}]
    # FCL.050-Extras aus dem PDF-Abgleich
    assert r['carryover_min'] == 7800
    lh500 = [e for e in r['entries'] if e['flight'] == 'LH500'][0]
    assert lh500['pic_name'] == 'MUSTERMANN, M.'
    # FCL.050: FSTD-Zeit zählt NICHT in die Flug-Totals/Muster-Summen
    assert r['totals']['block_min'] == 520 + 450 + 60 + 630
    assert not any(t['type'] == 'RE359' for t in r['by_type'])


def test_career_totals_include_aggregate_time_and_landing_carryovers():
    _seed()
    blob = dict(IMPORT_BLOB)
    blob['meta'] = {
        **IMPORT_BLOB['meta'],
        'carryover_ldg_day': 1050,
        'carryover_ldg_night': 7,
        'carryover_landings': 1057,
    }
    _seed_import(blob)
    r = _get()
    assert r['carryover_min'] == 7800
    assert r['carryover_ldg_day'] == 1050
    assert r['carryover_ldg_night'] == 7
    assert r['carryover_landings'] == 1057
    assert r['career_totals']['block_min'] == \
        r['totals']['block_min'] + 7800
    assert r['career_totals']['landings'] == \
        r['totals']['landings'] + 1057


def test_sim_sessions_empty_without_import():
    _seed()
    r = _get()
    assert r['sim_sessions'] == [] and r['sim_total_min'] == 0
    assert r['carryover_min'] == 0


def test_by_year_aggregation_includes_import():
    _seed()
    _seed_import()
    r = _get()
    by = {y['year']: y for y in r['by_year']}
    # absteigend sortiert, Import-Jahre enthalten
    assert [y['year'] for y in r['by_year']] == ['2026', '2019', '2014']
    assert by['2019'] == {'year': '2019', 'legs': 1, 'block_min': 630,
                          'landings': 1}
    assert by['2026']['legs'] == 3
    # Platzhalter-Leg 2014 ohne Blockzeit zählt als Leg, nicht als Stunden
    assert by['2014'] == {'year': '2014', 'legs': 1, 'block_min': 0,
                          'landings': 0}


# ── Anreicherung asynchron (2026-07-27) ─────────────────────────────────────
# Vorher lief die LH-/Board-Anreicherung SYNCHRON im Request (bis 60 Calls).
# Bei einem Roster mit vielen Legs ohne Reg/Typ waren das kalt bis zu 67 s,
# und der Edge bricht bei ~20 s mit einer 404-Seite ab. Diese Tests halten
# fest, dass der Request nicht mehr auf die Anreicherung wartet.

def test_enrichment_does_not_block_request(monkeypatch):
    """Kein einziger _flight_facts_from_obs-Aufruf im Request-Thread."""
    _seed()
    import blueprints.aerox_data_blueprint as bp
    calls = []

    def _boom(flight_no, date, dep_iata=None, arr_iata=None, **kw):
        calls.append(flight_no)
        raise AssertionError('Anreicherung darf den Request nicht blockieren')

    monkeypatch.setattr(bp, '_flight_facts_from_obs', _boom)
    started = []
    monkeypatch.setattr(backend, '_logbook_enrich_async',
                        lambda tok, wanted: started.append(list(wanted)))

    r = _get()
    assert r['ok'] is True
    assert calls == []
    # LH222 hat im Seed keine Reg → genau dieses Leg gehört in die Warteschlange
    assert len(started) == 1
    assert [w[1] for w in started[0]] == ['LH222']
    assert r['enrich_pending'] == 1
    assert r['enrich_capped'] is False


def test_enrichment_uses_persisted_facts_cache(monkeypatch):
    """Was der Hintergrund-Worker geschrieben hat, füllt den nächsten Request —
    ohne erneuten externen Call."""
    _seed()
    import time as _t
    # Leg-Key trägt den ROSTER-Tag (03.05.), nicht das Abflugdatum (04.05.)
    key = backend._logbook_leg_key('2026-05-03', 'LH222', 'FRA', 'MUC')
    backend._logbook_facts_save(TOKEN, {
        key: {'reg': 'D-AINA', 'type': 'A20N', 'at': int(_t.time())}})
    monkeypatch.setattr(backend, '_logbook_enrich_async',
                        lambda tok, wanted: None)
    try:
        r = _get()
        e = {x['flight']: x for x in r['entries']}
        assert e['LH222']['reg'] == 'D-AINA'      # Lücke aus dem Cache gefüllt
        # …aber der Sektor bleibt Herr über das, was er selbst weiß:
        # der Cache sagt A20N, der Roster 32N → Roster gewinnt.
        assert e['LH222']['type'] == '32N'
        assert r['enrich_pending'] == 0
    finally:
        p = backend._logbook_facts_path(TOKEN)
        if p and os.path.exists(p):
            os.remove(p)


def test_enrichment_cache_expires(monkeypatch):
    """Uralter Cache-Eintrag zählt nicht mehr — Leg geht zurück in die Queue."""
    _seed()
    # Leg-Key trägt den ROSTER-Tag (03.05.), nicht das Abflugdatum (04.05.)
    key = backend._logbook_leg_key('2026-05-03', 'LH222', 'FRA', 'MUC')
    backend._logbook_facts_save(TOKEN, {
        key: {'reg': 'D-ALT', 'type': 'OLD', 'at': 1}})   # 1970
    wanted = []
    monkeypatch.setattr(backend, '_logbook_enrich_async',
                        lambda tok, w: wanted.extend(w))
    try:
        r = _get()
        e = {x['flight']: x for x in r['entries']}
        assert e['LH222']['reg'] is None
        assert [w[1] for w in wanted] == ['LH222']
    finally:
        p = backend._logbook_facts_path(TOKEN)
        if p and os.path.exists(p):
            os.remove(p)


def test_enrich_worker_runs_once_per_token(monkeypatch):
    """Parallele Aufrufe desselben Tokens starten nur EINEN Worker — sonst
    feuern sie dieselben LH-Calls, und der Throttle serialisiert global."""
    import blueprints.aerox_data_blueprint as bp
    seen = []
    monkeypatch.setattr(bp, '_flight_facts_from_obs',
                        lambda fn, d, a=None, b=None, **kw: seen.append(fn) or {})
    backend._LOGBOOK_ENRICH_RUNNING.add('AT-BUSY')
    try:
        backend._logbook_enrich_async('AT-BUSY',
                                      [('k', 'LH1', '2026-05-01', 'FRA', 'MUC')])
        assert seen == []
    finally:
        backend._LOGBOOK_ENRICH_RUNNING.discard('AT-BUSY')


# ── Ist-Blockzeit statt BLZ68-Durchschnitt (Tester-Meldung 2026-08-10) ──────
# Die Spalte „BLOCK ZEIT" der LH-Flugstundenübersicht ist die
# BLZ68-DURCHSCHNITTSZEIT. iOS rechnet seit 10.08. clientseitig die Differenz
# der Ist-Zeiten (`FlugbuchPresentation.effectiveBlockMinutes`); der Server
# muss dieselbe Regel fahren, sonst widersprechen sich App-Summen und Export.

BLZ68_BLOB = {'legs': [
    # BLZ68 235 min, tatsächlich geflogen 4:12 = 252 min
    {'date': '2026-06-01', 'flight': 'LH620', 'from': 'FRA', 'to': 'TLV',
     'dep_iso': '2026-06-01T09:00:00+00:00',
     'arr_iso': '2026-06-01T13:12:00+00:00',
     'block_min': 235, 'type': 'A21N'},
    # Mitternachts-Wrap über die Offsets: 20:40Z → 01:05Z = 265 min
    {'date': '2026-06-02', 'flight': 'LH777', 'from': 'TLV', 'to': 'FRA',
     'dep_iso': '2026-06-02T23:40:00+03:00',
     'arr_iso': '2026-06-03T03:05:00+02:00',
     'block_min': 235, 'type': 'A21N'},
    # keine Ist-Zeiten → gespeicherter Wert bleibt stehen (nichts erfunden)
    {'date': '2026-06-04', 'flight': 'LH999', 'from': 'FRA', 'to': 'MUC',
     'block_min': 60, 'type': 'A21N'},
], 'sim': []}


def test_effective_block_min_matches_ios_rule():
    f = backend._logbook_effective_block_min
    # Ist-Differenz schlägt die importierte Durchschnittszahl (gleicher Fall
    # wie FlugbuchPresentationTests.testEffectiveBlockUsesRealOutInDifference…)
    assert f('2026-08-03T16:55:00Z', '2026-08-04T02:05:00Z', 999) == 550
    # Mitternachts-Wrap über Offsets
    assert f('2026-05-03T18:00:00-04:00', '2026-05-04T07:30:00+02:00', 1) == 450
    # fehlende/kaputte/negative Zeiten → Fallback unverändert (auch None)
    assert f(None, '2026-08-04T02:05:00Z', 77) == 77
    assert f('quatsch', 'auch quatsch', 77) == 77
    assert f('2026-08-04T03:05:00Z', '2026-08-04T02:05:00Z', 88) == 88
    assert f('2026-08-04T03:05:00Z', '2026-08-04T03:05:00Z', None) is None
    # NAIVE Zeiten sind nicht sicher als Instant lesbar → Fallback (iOS
    # ISO8601DateFormatter scheitert an ihnen ebenfalls)
    assert f('2026-08-03T16:55:00', '2026-08-03T18:55:00', 42) == 42
    # genau 24h zählt noch, mehr nicht
    assert f('2026-08-03T00:00:00Z', '2026-08-04T00:00:00Z', 5) == 1440
    assert f('2026-08-03T00:00:00Z', '2026-08-04T00:01:00Z', 5) == 5


def test_blz68_average_never_wins_over_measured_times():
    _seed()
    _seed_import(BLZ68_BLOB)
    r = _get()
    e = {x['flight']: x for x in r['entries']}
    assert e['LH620']['block_min'] == 252      # nicht 235 (BLZ68)
    assert e['LH777']['block_min'] == 265      # Mitternachts-Wrap
    assert e['LH999']['block_min'] == 60       # ohne Zeiten: Fallback
    # Summen ziehen dieselbe Zahl: Roster-Legs (520+450+60) + Import
    assert r['totals']['block_min'] == 1030 + 252 + 265 + 60
    bt = {t['type']: t for t in r['by_type']}
    assert bt['A21N']['legs'] == 3 and bt['A21N']['block_min'] == 577
    by_year = {y['year']: y for y in r['by_year']}
    assert by_year['2026']['block_min'] == 1030 + 577
