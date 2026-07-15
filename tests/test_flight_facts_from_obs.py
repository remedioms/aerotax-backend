"""Unified Flight-Info P0 — der geteilte Board-Fakten-Mapper `_obs_rows_to_facts`
+ die Datums-Disziplin von `_flight_facts_from_obs`.

Beweist den DEP+ARR-Merge (die eine Stelle, die künftig alle Screens lesen):
Soll/Ist Ab+Ankunft, Gate, Delay, Reg/Typ — nur echte Werte, None/'' weg.
Seit dem Zeit-Normalisierungs-P0 zusätzlich: sched/esti kommen als ISO MIT
Station-Offset raus (Wanduhr bleibt Station-lokal lesbar, Offset macht sie
eindeutig) — egal ob die Row bare 'HH:MM', naive Lokal-ISO oder Offset-ISO trug.
"""
import types

import blueprints.aerox_data_blueprint as axd
from blueprints.aerox_data_blueprint import (_flight_facts_from_obs,
                                             _obs_rows_to_facts)


def test_dep_and_arr_rows_merge_full_facts():
    # Reale Form (LH146 FRA→NUE, 2026-07-09): DEP-Row FRA + ARR-Row NUE#ARR.
    # Formate absichtlich gemischt: bare 'HH:MM', Offset-ISO ohne Doppelpunkt,
    # naive Lokal-ISO → ALLE kommen als Station-Offset-ISO raus.
    dep = {'airport': 'FRA', 'flight': 'LH146', 'dest_iata': 'NUE',
           'date': '2026-07-09',
           'sched': '16:50', 'esti': '2026-07-09T17:30:00+0200', 'gate': 'A58',
           'terminal': '1', 'status': 'Abgeflogen', 'max_delay_min': 30,
           'cancelled': False, 'reg': 'DAIBH', 'type_code': 'A319'}
    arr = {'airport': 'NUE#ARR', 'flight': 'LH146', 'dest_iata': 'FRA',
           'date': '2026-07-09',
           'sched': '17:35', 'esti': '2026-07-09T18:20:00', 'gate': None,
           'terminal': None, 'status': 'delayed', 'max_delay_min': 45,
           'cancelled': False}
    f = _obs_rows_to_facts(dep, arr)
    # Abflugseite — bare '16:50' → Servicedatum + FRA-Offset (Juli = +02:00)
    assert f['sched_dep'] == '2026-07-09T16:50:00+02:00'
    assert f['est_dep'] == '2026-07-09T17:30:00+02:00'
    assert f['gate'] == 'A58'
    assert f['dep_delay_min'] == 30
    assert f['reg'] == 'DAIBH'
    assert f['type'] == 'A319'
    # Ankunftseite — Station-TZ der ARR-Row = ZIEL (NUE); naive ISO kriegt Offset
    assert f['sched_arr'] == '2026-07-09T17:35:00+02:00'
    assert f['est_arr'] == '2026-07-09T18:20:00+02:00'
    assert f['arr_delay_min'] == 45
    # Route steckt auch in den Obs (DEP-Row airport→dest_iata)
    assert f['dep_iata'] == 'FRA'
    assert f['arr_iata'] == 'NUE'


def test_route_from_arr_row_only():
    # Nur ARR-Row (NUE#ARR, dest_iata=FRA=Herkunft) → Route trotzdem ableitbar.
    arr = {'airport': 'NUE#ARR', 'flight': 'LH146', 'dest_iata': 'FRA',
           'date': '2026-07-09',
           'sched': '17:35', 'esti': '2026-07-09T18:20:00'}
    f = _obs_rows_to_facts(None, arr)
    assert f['arr_iata'] == 'NUE'
    assert f['dep_iata'] == 'FRA'
    assert f['sched_arr'] == '2026-07-09T17:35:00+02:00'
    assert f['est_arr'] == '2026-07-09T18:20:00+02:00'


def test_dep_only_no_arr_row():
    # Row OHNE Servicedatum: bare Zeit ist nicht eindeutig datierbar →
    # Rohwert bleibt erhalten (nichts erfinden), Rest wie gehabt.
    dep = {'airport': 'FRA', 'flight': 'LH146', 'sched': '16:50', 'esti': None,
           'gate': '', 'status': '', 'max_delay_min': None}
    f = _obs_rows_to_facts(dep, None)
    assert f['sched_dep'] == '16:50'
    assert 'est_dep' not in f       # None → weggelassen (nie erfunden)
    assert 'gate' not in f          # '' → weggelassen
    assert 'dep_delay_min' not in f  # None → weggelassen
    assert 'sched_arr' not in f      # keine ARR-Row → keine Ankunft
    # Unbekannte Station-TZ → ebenfalls Rohwert behalten
    f2 = _obs_rows_to_facts({'airport': 'XX', 'date': '2026-07-09',
                             'sched': '16:50'}, None)
    assert f2['sched_dep'] == '16:50'


def test_midnight_wrap_est_after_zero():
    # est '00:30' bei sched '23:50' = NACH Mitternacht → +1 Tag (est<sched−4h).
    dep = {'airport': 'FRA', 'flight': 'LH146', 'date': '2026-07-09',
           'sched': '23:50', 'esti': '00:30'}
    f = _obs_rows_to_facts(dep, None)
    assert f['sched_dep'] == '2026-07-09T23:50:00+02:00'
    assert f['est_dep'] == '2026-07-10T00:30:00+02:00'


def test_overnight_arrival_uses_next_local_day_not_previous_rotation(monkeypatch):
    """LH455: DEP 14.07 SFO, passende ARR ist 15.07 FRA. Die ARR-Row vom
    14.07 gehoert zur vorigen Tagesrotation und darf nicht gewinnen."""
    dep = {'airport': 'SFO', 'flight': 'LH455', 'dest_iata': 'FRA',
           'date': '2026-07-14', 'sched': '14:40', 'esti': '15:34'}
    previous_arr = {'airport': 'FRA#ARR', 'flight': 'LH455',
                    'dest_iata': 'SFO', 'date': '2026-07-14',
                    'sched': '10:25', 'esti': '10:35'}
    matching_arr = {'airport': 'FRA#ARR', 'flight': 'LH455',
                    'dest_iata': 'SFO', 'date': '2026-07-15',
                    'sched': '10:25', 'esti': '10:35'}
    monkeypatch.setattr(axd, '_sb',
                        lambda: _FakeSB([dep, previous_arr, matching_arr]))

    f = _flight_facts_from_obs('LH455', '2026-07-14', 'SFO', 'FRA')

    assert f['sched_dep'] == '2026-07-14T14:40:00-07:00'
    assert f['sched_arr'] == '2026-07-15T10:25:00+02:00'
    assert f['est_arr'] == '2026-07-15T10:35:00+02:00'
    assert not f.get('stale')


def test_cancelled_flag_from_either_side():
    assert _obs_rows_to_facts({'cancelled': True}, None).get('cancelled') is True
    assert _obs_rows_to_facts(None, {'cancelled': True}).get('cancelled') is True
    assert _obs_rows_to_facts(None, None) == {}


# ── _flight_facts_from_obs: Datums-Disziplin (P1-3) ─────────────────────────

class _FakeQ:
    def __init__(self, rows):
        self._rows = rows

    def __getattr__(self, name):
        # select/in_/eq/order/limit … → chainbar
        def _chain(*a, **kw):
            return self
        return _chain

    def execute(self):
        return types.SimpleNamespace(data=self._rows)


class _FakeSB:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        return _FakeQ(self._rows)


def test_yday_gate_row_does_not_beat_today(monkeypatch):
    """Der Kernfall: eine GESTRIGE Final-Row (mit Gate/Ist-Zeit) desselben
    täglichen Flugs darf die HEUTIGE Row nicht schlagen — Rows werden nach
    Datum partitioniert, angefragtes Datum strikt zuerst."""
    yday_final = {'airport': 'FRA', 'flight': 'LH146', 'dest_iata': 'NUE',
                  'date': '2026-07-08', 'sched': '16:50', 'esti': '17:42',
                  'gate': 'A58', 'reg': 'DAIBH'}
    today_plain = {'airport': 'FRA', 'flight': 'LH146', 'dest_iata': 'NUE',
                   'date': '2026-07-09', 'sched': '16:50', 'esti': None,
                   'gate': None, 'reg': None}
    # updated_at desc: die gestrige Final-Row liegt „frischer" im Result —
    # trotzdem muss die heutige gewinnen.
    monkeypatch.setattr(axd, '_sb',
                        lambda: _FakeSB([yday_final, today_plain]))
    f = _flight_facts_from_obs('LH146', '2026-07-09')
    assert f['sched_dep'] == '2026-07-09T16:50:00+02:00'
    assert 'est_dep' not in f          # gestrige Ist-Zeit darf NICHT durchsickern
    assert 'gate' not in f             # gestriges Gate ebenso wenig
    assert not f.get('stale')


def test_today_departure_drops_yday_arrival_side(monkeypatch):
    """D-AIZD/LH1346 live regression: DEP stammt vom angefragten Tag, die einzige
    ARR-Row aber vom Vortag. Die ARR-Seite (inkl. Landed/Delay/Gate) muss komplett
    fehlen, damit der on-demand Resolver free-first → paid-last nachziehen kann."""
    today_dep = {'airport': 'FRA', 'flight': 'LH1346', 'dest_iata': 'WAW',
                 'date': '2026-07-14', 'sched': '07:10', 'esti': '09:00',
                 'gate': 'A17', 'status': 'Abgeflogen', 'max_delay_min': 110}
    yday_arr = {'airport': 'WAW#ARR', 'flight': 'LH1346', 'dest_iata': 'FRA',
                'date': '2026-07-13', 'sched': '08:55', 'esti': None,
                'gate': 'Tape 6', 'status': 'Landed', 'max_delay_min': 0}
    monkeypatch.setattr(axd, '_sb', lambda: _FakeSB([today_dep, yday_arr]))

    f = _flight_facts_from_obs('LH1346', '2026-07-14')

    assert f['sched_dep'] == '2026-07-14T07:10:00+02:00'
    assert f['dep_iata'] == 'FRA' and f['arr_iata'] == 'WAW'
    for key in ('sched_arr', 'est_arr', 'arr_status', 'arr_delay_min',
                'arr_gate', 'arr_terminal'):
        assert not f.get(key)
    assert not f.get('stale')


def test_overnight_departure_query_drops_foreign_morning_arrival(monkeypatch):
    """LH423 BOS→FRA (Owner/Fable 2026-07-15): das Über-Nacht-Rückleg startet am
    15. abends in BOS und landet am 16. in FRA. Für den Abflug-Tag-Query
    (date=2026-07-15, dep=BOS) existiert NUR eine ARR-Row am gleichen Kalendertag
    — die FRA-Ankunft (07:44) der GESTRIGEN Rotation. Ohne DEP-Row (BOS nicht
    geharvestet) darf diese physikalisch unmögliche Morgen-Ankunft NICHT als heutige
    Ist-Zeit durchsickern: ein Flug, der am 15. von BOS startet, kann FRA frühestens
    Stunden später erreichen. Die freie Zeiten-Kette lieferte sonst esti=07:44."""
    foreign_arr = {'airport': 'FRA#ARR', 'flight': 'LH423', 'dest_iata': 'BOS',
                   'date': '2026-07-15', 'sched': '07:20', 'esti': '07:44',
                   'status': 'Gelandet', 'max_delay_min': 24}
    monkeypatch.setattr(axd, '_sb', lambda: _FakeSB([foreign_arr]))
    monkeypatch.setattr(axd, '_tail_active_guard', lambda r: True)

    f = _flight_facts_from_obs('LH423', '2026-07-15', 'BOS', 'FRA')

    for key in ('sched_arr', 'est_arr', 'arr_status', 'arr_delay_min'):
        assert not f.get(key)


def test_same_day_arrival_only_kept_when_physically_possible(monkeypatch):
    """Gegentest (kein Über-Scrub): eine gleichtägige Innereuropa-Ankunft
    (LH146 FRA→NUE, 17:35) ist für einen FRA-Abflug am selben Tag physikalisch
    plausibel → die ARR-only-Row bleibt erhalten."""
    arr = {'airport': 'NUE#ARR', 'flight': 'LH146', 'dest_iata': 'FRA',
           'date': '2026-07-15', 'sched': '17:35', 'esti': '17:42',
           'status': 'Landed'}
    monkeypatch.setattr(axd, '_sb', lambda: _FakeSB([arr]))
    monkeypatch.setattr(axd, '_tail_active_guard', lambda r: True)

    f = _flight_facts_from_obs('LH146', '2026-07-15', 'FRA', 'NUE')

    assert f['sched_arr'] == '2026-07-15T17:35:00+02:00'
    assert f['est_arr'] == '2026-07-15T17:42:00+02:00'


def test_arrival_airport_query_keeps_flown_arrival(monkeypatch):
    """Gegentest 'gestern gestartet, heute gelandet' PER ANKUNFTS-Airport-Query:
    LH455 SFO→FRA landet früh am 15. in FRA. Fragt der Client per Ankunftstag ohne
    gebundenen Abflug-Airport, darf die geflogene Ankunft NICHT verworfen werden —
    das Abflugtag-Gate feuert nur bei explizit gebundenem dep_iata."""
    arr = {'airport': 'FRA#ARR', 'flight': 'LH455', 'dest_iata': 'SFO',
           'date': '2026-07-15', 'sched': '10:25', 'esti': '10:35',
           'status': 'Gelandet'}
    monkeypatch.setattr(axd, '_sb', lambda: _FakeSB([arr]))
    monkeypatch.setattr(axd, '_tail_active_guard', lambda r: True)

    f = _flight_facts_from_obs('LH455', '2026-07-15')      # kein dep_iata

    assert f['sched_arr'] == '2026-07-15T10:25:00+02:00'
    assert f['est_arr'] == '2026-07-15T10:35:00+02:00'


def test_yday_fallback_marks_stale(monkeypatch):
    """Overnight-Fall: das angefragte Datum ist leer → yday-Row als Fallback,
    aber transparent als stale/obs_date markiert."""
    yday_only = {'airport': 'FRA', 'flight': 'LH146', 'dest_iata': 'NUE',
                 'date': '2026-07-08', 'sched': '16:50', 'esti': None,
                 'gate': 'A58'}
    monkeypatch.setattr(axd, '_sb', lambda: _FakeSB([yday_only]))
    f = _flight_facts_from_obs('LH146', '2026-07-09')
    assert f['gate'] == 'A58'
    assert f['sched_dep'] == '2026-07-08T16:50:00+02:00'
    assert f.get('stale') is True
    assert f.get('obs_date') == '2026-07-08'


def test_enrich_ignores_stale_yday_facts(monkeypatch):
    """P5-Nachfix: liefert _flight_facts_from_obs nur einen Vortags-Fallback
    (stale=True), darf _enrich_flight_status_with_obs die gestrige Geister-
    Ankunft NICHT in den Detail-Screen füllen."""
    stale = {'sched_arr': '2026-07-08T18:05:00+02:00',
             'est_arr': '2026-07-08T18:20:00+02:00',
             'arr_status': 'Gelandet', 'dep_status': 'Abgeflogen',
             'stale': True, 'obs_date': '2026-07-08'}
    monkeypatch.setattr(axd, '_flight_facts_from_obs', lambda *a, **k: stale)
    monkeypatch.setattr(axd, '_flight_times_free_first', lambda *a, **k: {})
    out = axd._enrich_flight_status_with_obs(
        {'flight': 'LH146', 'dep_iata': 'FRA', 'arr_iata': 'NUE'},
        date='2026-07-09')
    assert not out.get('sched_arr')
    assert not out.get('est_arr')
    assert not out.get('status_category')     # kein Geister-"arrived"


def test_enrich_fills_fresh_facts(monkeypatch):
    """Frische Fakten füllen weiterhin (Gate darf nicht alles blocken)."""
    fresh = {'sched_arr': '2026-07-09T18:05:00+02:00',
             'est_arr': '2026-07-09T18:20:00+02:00', 'arr_status': 'Gelandet'}
    monkeypatch.setattr(axd, '_flight_facts_from_obs', lambda *a, **k: fresh)
    monkeypatch.setattr(axd, '_flight_times_free_first', lambda *a, **k: {})
    out = axd._enrich_flight_status_with_obs(
        {'flight': 'LH146', 'dep_iata': 'FRA', 'arr_iata': 'NUE'},
        date='2026-07-09')
    assert out['sched_arr'] == '2026-07-09T18:05:00+02:00'
    assert out['status_category'] == 'arrived'


def test_enrich_refills_missing_arrival_via_free_first_paid_backup(monkeypatch):
    """Ein vorhandener Abflug darf den zentralen Zeiten-Resolver nicht sperren.
    Regressionsfall D-AIZD/LH1346: die stale Vortages-Ankunft wird verworfen,
    danach muss die einzelne ARR-Luecke weiterhin free-first → paid-last laufen.
    Der bereits richtige Abflug bleibt unangetastet."""
    calls = []
    monkeypatch.setattr(axd, '_flight_facts_from_obs', lambda *a, **k: {})
    monkeypatch.setattr(axd, '_grpc_times_free', lambda *a, **k: None)

    def _paid(*args, **kwargs):
        calls.append((args, kwargs))
        return {'sched_dep': '2026-07-14T12:00:00Z',
                'sched_arr': '2026-07-14T14:05:00Z'}

    monkeypatch.setattr(axd, '_fr24_flight_by_number', _paid)
    out = axd._enrich_flight_status_with_obs(
        {'flight': 'LH1346', 'callsign': 'DLH2EW',
         'dep_iata': 'FRA', 'arr_iata': 'WAW',
         'sched_dep': '2026-07-14T13:05:00+02:00'},
        date='2026-07-14', allow_paid=True)

    assert out['sched_dep'] == '2026-07-14T13:05:00+02:00'
    assert out['sched_arr'] == '2026-07-14T16:05:00'
    assert len(calls) == 1


def test_enrich_partial_times_keeps_paid_disabled_on_free_only_path(monkeypatch):
    """Der OR-Fix erweitert nicht den Kostenumfang des Aggregats: bei
    allow_paid=False wird die einzelne ARR-Luecke gratis versucht, aber der
    paid FR24-Backup bleibt aus."""
    monkeypatch.setattr(axd, '_flight_facts_from_obs', lambda *a, **k: {})
    monkeypatch.setattr(axd, '_grpc_times_free', lambda *a, **k: None)
    paid = []
    monkeypatch.setattr(axd, '_fr24_flight_by_number',
                        lambda *a, **k: paid.append(True) or {})

    out = axd._enrich_flight_status_with_obs(
        {'flight': 'LH1346', 'callsign': 'DLH2EW',
         'dep_iata': 'FRA', 'arr_iata': 'WAW',
         'sched_dep': '2026-07-14T13:05:00+02:00'},
        date='2026-07-14', allow_paid=False)

    assert not out.get('sched_arr')
    assert paid == []


# ─── Free/Paid-Zeiten-Memo: Kostenmodus ist Teil der Wahrheit ─────────────

def test_free_only_miss_does_not_suppress_later_paid_lookup(monkeypatch):
    """Ein free-only Miss des Detail-Aggregats darf den anschliessenden
    Standalone-Aufruf mit allow_paid=True nicht fuenf Minuten lang blockieren."""
    paid_calls = []
    monkeypatch.setattr(axd, '_grpc_times_free', lambda *a, **k: None)

    def _paid(*args, **kwargs):
        paid_calls.append((args, kwargs))
        return {'sched_dep': '2026-07-14T12:00:00',
                'sched_arr': '2026-07-14T13:00:00'}

    monkeypatch.setattr(axd, '_fr24_flight_by_number', _paid)

    free = axd._flight_times_free_first(
        'LH146', '2026-07-14', 'FRA', 'NUE', allow_paid=False)
    paid = axd._flight_times_free_first(
        'LH146', '2026-07-14', 'FRA', 'NUE', allow_paid=True)

    assert free == {}
    assert paid['sched_dep'] == '2026-07-14T12:00:00'
    assert len(paid_calls) == 1
    assert ('LH146', '2026-07-14', False, False) in axd._FREE_TIMES_MEMO
    assert ('LH146', '2026-07-14', True, False) in axd._FREE_TIMES_MEMO


def test_paid_memo_does_not_leak_into_free_only_lookup(monkeypatch):
    """Der umgekehrte Aufruf bleibt ebenfalls getrennt: free-only gibt nicht
    still Daten aus dem paid Memo zurueck."""
    monkeypatch.setattr(axd, '_grpc_times_free', lambda *a, **k: None)
    monkeypatch.setattr(
        axd, '_fr24_flight_by_number',
        lambda *a, **k: {'sched_dep': '2026-07-14T12:00:00',
                         'sched_arr': '2026-07-14T13:00:00'})

    paid = axd._flight_times_free_first(
        'LH146', '2026-07-14', 'FRA', 'NUE', allow_paid=True)
    free = axd._flight_times_free_first(
        'LH146', '2026-07-14', 'FRA', 'NUE', allow_paid=False)

    assert paid['sched_arr'] == '2026-07-14T13:00:00'
    assert free == {}


def test_operational_detail_can_fill_estimates_when_schedule_is_complete(monkeypatch):
    """LH422-Klasse: vorhandene Planzeiten duerfen den gezielten Paid-Backup
    fuer fehlende Ist/Erwartet-Zeiten nicht sperren."""
    monkeypatch.setattr(
        axd, '_grpc_times_free',
        lambda *a, **k: {'sched_dep': 1784035800, 'sched_arr': 1784043600})
    monkeypatch.setattr(
        axd, '_fr24_flight_by_number',
        lambda *a, **k: {'est_dep': '2026-07-14T13:42:00Z',
                         'est_arr': '2026-07-14T15:52:00Z'})

    out = axd._flight_times_free_first(
        'LH422', '2026-07-14', 'FRA', 'BOS', allow_paid=True,
        require_operational=True)

    assert out.get('est_dep')
    assert out.get('est_arr')


# ── FlightState-Engine → status_category (Owner 2026-07-13) ───────────────────
# _status_category_from_facts leitet status_category NICHT mehr per roher
# Substring-Suche ab, sondern über die EINE FlightState-Engine (obs_from_board_
# merged → resolve_flight_state). Dieselbe Wahrheit wie crew_state/flights_live.
# NOW = 2026-07-09T04:00:00Z (fest, deterministisch).
_NOW = 1783584000


def test_status_category_taxi_offblock_not_enroute():
    """Ghost-Fix: ein dep-seitiges „Abgeflogen" = OFF-BLOCK, nicht airborne. Die
    Engine macht daraus TAXI_OUT → status_category bleibt LEER (kein Legacy-Wert).
    Die alte Substring-Heuristik hätte fälschlich 'enroute' gesetzt (Geister-
    Airborne eines noch rollenden Fliegers)."""
    facts = {'dep_iata': 'FRA', 'arr_iata': 'GVA', 'dep_status': 'Abgeflogen',
             'sched_dep': '2026-07-09T05:50:00+02:00',
             'sched_arr': '2026-07-09T06:55:00+02:00'}
    flight = {'flight': 'LH2557', 'dep_iata': 'FRA', 'arr_iata': 'GVA'}
    assert axd._status_category_from_facts(flight, facts, now=_NOW) is None


def test_status_category_bogus_early_landing_rejected():
    """PLAUSI-GATE: ein 11h-Flug (LH454 FRA→SFO), dessen Ankunftstafel physisch
    unmöglich früh „Gelandet" trägt (sched_arr Stunden VORAUS), darf NICHT
    'arrived' werden. Die rohe Substring-Suche hätte sofort 'arrived' gesetzt."""
    facts = {'dep_iata': 'FRA', 'arr_iata': 'SFO', 'dep_status': 'Abgeflogen',
             'arr_status': 'Gelandet',
             'sched_dep': '2026-07-09T10:30:00+02:00',
             'sched_arr': '2026-07-09T13:35:00-07:00'}   # 20:35Z, weit nach NOW
    flight = {'flight': 'LH454', 'dep_iata': 'FRA', 'arr_iata': 'SFO'}
    assert axd._status_category_from_facts(flight, facts, now=_NOW) is None


def test_status_category_plausible_landing_arrived():
    """Gegenprobe: eine plausible Landung (Soll-Ankunft liegt VOR now, Board
    „Gelandet") wird korrekt 'arrived'."""
    facts = {'dep_iata': 'FRA', 'arr_iata': 'NUE', 'arr_status': 'Gelandet',
             'sched_arr': '2026-07-09T02:00:00+02:00'}   # 00:00Z, vor NOW(04:00Z)
    flight = {'flight': 'LH146', 'dep_iata': 'FRA', 'arr_iata': 'NUE'}
    assert axd._status_category_from_facts(flight, facts, now=_NOW) == 'arrived'


def test_status_category_cancelled():
    """Board-Cancel → 'cancelled' (hart, schlägt alles)."""
    facts = {'dep_iata': 'MUC', 'arr_iata': 'JFK', 'cancelled': True}
    flight = {'flight': 'LH444', 'dep_iata': 'MUC', 'arr_iata': 'JFK'}
    assert axd._status_category_from_facts(flight, facts, now=_NOW) == 'cancelled'


def test_enrich_taxi_status_not_enroute(monkeypatch):
    """End-to-end über _enrich_flight_status_with_obs: dep-seitiges „Abgeflogen"
    ohne Ankunfts-Landung ⇒ status_category bleibt leer (nicht 'enroute')."""
    facts = {'dep_iata': 'FRA', 'arr_iata': 'GVA', 'dep_status': 'Abgeflogen',
             'sched_dep': '2099-01-01T12:00:00+01:00',
             'sched_arr': '2099-01-01T13:00:00+01:00'}
    monkeypatch.setattr(axd, '_flight_facts_from_obs', lambda *a, **k: dict(facts))
    monkeypatch.setattr(axd, '_flight_times_free_first', lambda *a, **k: {})
    out = axd._enrich_flight_status_with_obs(
        {'flight': 'LH2557', 'dep_iata': 'FRA', 'arr_iata': 'GVA'},
        date='2099-01-01')
    assert not out.get('status_category')     # kein Geister-'enroute'


# ── Selbstkonsistenz-Invariante _scrub_wrong_day_esti (Owner/Fable 2026-07-15) ──
# Eine Antwort darf sich nicht selbst widersprechen: eine absolute Ist-Ankunft, die
# > 90 min VOR dem Soll-Abflug (oder > 6 h vor der Soll-Ankunft) DERSELBEN Antwort
# liegt, gehört zu einer Fremd-Tages-Instanz → Ist-Felder scrubben, Soll behalten.

def test_scrub_lh423_wrong_day_esti_arr_before_own_dep():
    """Exakt der Live-Payload LH423 BOS→FRA: sched_arr 16.07 06:50, aber
    est_arr 15.07 07:44 (Vortages-Instanz) — ~23 h VOR der eigenen Soll-Ankunft
    und VOR dem Soll-Abflug (17:45 BOS). Ist-Felder scrubben, Soll bleibt."""
    f = {'dep_iata': 'BOS', 'arr_iata': 'FRA',
         'sched_dep': '2026-07-15T17:45:00-04:00',
         'sched_arr': '2026-07-16T06:50:00+02:00',
         'est_arr': '2026-07-15T07:44:00+02:00',
         'arr_status': 'Gelandet', 'arr_delay_min': -1}
    axd._scrub_wrong_day_esti(f, service_date='2026-07-15')
    assert f.get('esti_scrubbed') is True
    assert 'est_arr' not in f
    assert 'arr_status' not in f
    assert 'arr_delay_min' not in f
    # Soll-Felder ehrlich behalten (nichts erfunden).
    assert f['sched_arr'] == '2026-07-16T06:50:00+02:00'
    assert f['sched_dep'] == '2026-07-15T17:45:00-04:00'


def test_scrub_self_contradictory_arr_row_via_mapper():
    """End-to-end über den Mapper: eine ARR-Row trägt sched 06:50 (16.07-Ankunft),
    aber ein dated esti 15.07 07:44 (Fremd-Rotation). Nach _obs_rows_to_facts +
    Scrub ist die widersprüchliche Ist-Ankunft weg, sched_arr bleibt."""
    dep = {'airport': 'BOS', 'flight': 'LH423', 'dest_iata': 'FRA',
           'date': '2026-07-15', 'sched': '17:45', 'esti': None}
    arr = {'airport': 'FRA#ARR', 'flight': 'LH423', 'dest_iata': 'BOS',
           'date': '2026-07-16', 'sched': '06:50',
           'esti': '2026-07-15T07:44:00+02:00', 'status': 'Gelandet',
           'max_delay_min': -1}
    f = axd._obs_rows_to_facts(dep, arr)
    assert f['est_arr'] == '2026-07-15T07:44:00+02:00'   # vor dem Scrub da
    axd._scrub_wrong_day_esti(f, service_date='2026-07-15')
    assert f.get('esti_scrubbed') is True
    assert 'est_arr' not in f
    assert f['sched_arr'] == '2026-07-16T06:50:00+02:00'
    assert f['sched_dep'] == '2026-07-15T17:45:00-04:00'


def test_scrub_keeps_normal_delay():
    """Gegentest: normale Verspätung (est_arr NACH sched_arr) — nicht scrubben."""
    f = {'dep_iata': 'FRA', 'arr_iata': 'NUE',
         'sched_dep': '2026-07-09T16:50:00+02:00',
         'sched_arr': '2026-07-09T17:35:00+02:00',
         'est_arr': '2026-07-09T18:20:00+02:00',
         'arr_status': 'delayed', 'arr_delay_min': 45}
    axd._scrub_wrong_day_esti(f, service_date='2026-07-09')
    assert not f.get('esti_scrubbed')
    assert f['est_arr'] == '2026-07-09T18:20:00+02:00'
    assert f['arr_delay_min'] == 45


def test_scrub_keeps_moderate_early_arrival():
    """Gegentest: Verfrühung 30 min (est_arr vor sched_arr, aber weit nach dem
    Soll-Abflug und innerhalb der 90-min-Marge/6-h-Grenze) — behalten."""
    f = {'dep_iata': 'FRA', 'arr_iata': 'NUE',
         'sched_dep': '2026-07-09T16:50:00+02:00',
         'sched_arr': '2026-07-09T17:35:00+02:00',
         'est_arr': '2026-07-09T17:05:00+02:00'}
    axd._scrub_wrong_day_esti(f, service_date='2026-07-09')
    assert not f.get('esti_scrubbed')
    assert f['est_arr'] == '2026-07-09T17:05:00+02:00'


def test_scrub_keeps_legit_overnight_next_day_arrival():
    """Gegentest: legitime Übernacht-Ankunft — est_arr am Folgetag (16.07 07:10),
    NACH dem Soll-Abflug (15.07 abends), ~20 min nach der Soll-Ankunft. Behalten."""
    f = {'dep_iata': 'BOS', 'arr_iata': 'FRA',
         'sched_dep': '2026-07-15T17:45:00-04:00',
         'sched_arr': '2026-07-16T06:50:00+02:00',
         'est_arr': '2026-07-16T07:10:00+02:00',
         'arr_status': 'Gelandet', 'arr_delay_min': 20}
    axd._scrub_wrong_day_esti(f, service_date='2026-07-15')
    assert not f.get('esti_scrubbed')
    assert f['est_arr'] == '2026-07-16T07:10:00+02:00'
    assert f['arr_status'] == 'Gelandet'


def test_scrub_arrival_only_query_uses_sched_arr_bound():
    """Gegentest 'reine Ankunfts-Query ohne Abflug-Anker': fehlt sched_dep UND
    est_dep, greift nur die 6-h-vor-sched_arr-Schranke. Eine plausibel geflogene
    Ankunft (10:35 vs Soll 10:25) bleibt erhalten."""
    f = {'arr_iata': 'FRA',
         'sched_arr': '2026-07-15T10:25:00+02:00',
         'est_arr': '2026-07-15T10:35:00+02:00',
         'arr_status': 'Gelandet'}
    axd._scrub_wrong_day_esti(f, service_date='2026-07-15')
    assert not f.get('esti_scrubbed')
    assert f['est_arr'] == '2026-07-15T10:35:00+02:00'


def test_scrub_fail_open_on_unparsable():
    """Fail-open: unparsbare/fehlende Zeiten → facts unverändert, kein Scrub."""
    f = {'dep_iata': 'BOS', 'arr_iata': 'FRA', 'est_arr': 'nonsense'}
    axd._scrub_wrong_day_esti(f, service_date='2026-07-15')
    assert not f.get('esti_scrubbed')
    assert f['est_arr'] == 'nonsense'


def test_flight_facts_from_obs_applies_self_consistency(monkeypatch):
    """Integration: _flight_facts_from_obs verdrahtet den Scrub am Merge-Ausgang.
    Eine widersprüchliche ARR-Row (sched morgen, esti gestern) liefert KEINE
    est_arr/arr_status mehr, aber die Soll-Ankunft bleibt."""
    dep = {'airport': 'BOS', 'flight': 'LH423', 'dest_iata': 'FRA',
           'date': '2026-07-15', 'sched': '17:45', 'esti': None}
    arr = {'airport': 'FRA#ARR', 'flight': 'LH423', 'dest_iata': 'BOS',
           'date': '2026-07-16', 'sched': '06:50',
           'esti': '2026-07-15T07:44:00+02:00', 'status': 'Gelandet',
           'max_delay_min': -1}
    monkeypatch.setattr(axd, '_sb', lambda: _FakeSB([dep, arr]))
    monkeypatch.setattr(axd, '_tail_active_guard', lambda r: True)
    f = _flight_facts_from_obs('LH423', '2026-07-15', 'BOS', 'FRA')
    assert f['sched_arr'] == '2026-07-16T06:50:00+02:00'
    assert not f.get('est_arr')
    assert not f.get('arr_status')
    assert not f.get('arr_delay_min')
