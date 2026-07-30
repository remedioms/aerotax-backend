"""Ankunfts-Seite von /api/ax/flight-live — die EINE Fakten-Quelle.

ROOT-CAUSE (Owner live bestätigt am eigenen Umlauf, LH454 FRA→SFO 2026-07-30):
`/api/ax/uflight/LH454` lieferte `est_arr 12:36-07:00`, `arr_gate G13`,
`arr_delay_min -4` — `/api/ax/flight-live/<token>` für denselben Flug dagegen
`sched_arr: None, est_arr: None, arr_delay_min: None, dest_gate: None`.

Grund: flight-live las die Ankunft AUSSCHLIESSLICH aus `_flight_obs_merged`,
und dessen arr-Seite ist board-/warehouse-gebunden. Für eine Outstation, deren
Ankunftstafel niemand scraped (SFO), gibt es schlicht keine arr-Row. Die
Ankunftswahrheit steckt für LH-Group-Flüge in den LH-Open-API-Fakten, die
`_flight_facts_from_obs` (die Quelle von uflight) bereits einmergt.

Gefixt über `_live_arrival_fields` (pur) + `_live_arrival_facts` (gated),
beides hier getestet. Die zwei Zusagen, die NICHT brechen dürfen:
  1. Es wird NICHTS erfunden — hat keine Quelle die Zahl, bleibt sie None.
  2. Der Poll-Pfad (iOS: alle 30–60 s) kostet KEINEN zusätzlichen
     LH-Gateway-Call. Der Stunden-Deckel ist real (Messung 30.07., Stunde 16
     UTC: 869 Calls durch, 1026 abgewiesen) — ein blockierender Call pro Poll
     wäre ein Quota-Killer.
"""
import pytest

import app as A
import blueprints.aerox_data_blueprint as BP


@pytest.fixture(autouse=True)
def _clear_caches():
    A._FLIGHT_MERGE_CACHE.clear()
    for name in ('_LIFECYCLE_MEMO', '_OBS_FACTS_MEMO'):
        try:
            getattr(BP, name).clear()
        except Exception:
            pass
    yield


@pytest.fixture
def client():
    A.app.testing = True
    return A.app.test_client()


# Board-Merge-Shape (`_flight_obs_merged`) mit VOLLSTÄNDIGER Ankunftsseite.
_BOARD_FULL = {
    'sched_arr': '2026-07-30T12:40:00+02:00',
    'esti_arr': '2026-07-30T12:52:00+02:00',
    'arr_delay_min': 12, 'delay_known': True,
    'gate_arr': 'B22',
}
# Board-Merge OHNE jede Ankunftsseite (Outstation ohne Tafel — der Owner-Fall).
_BOARD_BLIND = {
    'sched_dep': '2026-07-30T10:25:00+0200',
    'esti_dep': '2026-07-30T10:45:00+0200',
    'dep_delay_min': 20, 'delay_known': True, 'gate_dep': 'Z66',
    'sched_arr': None, 'esti_arr': None, 'arr_delay_min': None,
    'gate_arr': None,
}
# Fakten-Shape (`_flight_facts_from_obs`, LH-veredelt) — die echten LH454-Werte.
_FACTS_LH454 = {
    'sched_arr': '2026-07-30T12:40:00-07:00',
    'est_arr': '2026-07-30T12:36:00-07:00',
    'arr_delay_min': -4,
    'arr_gate': 'G13',
}


# ══════════════════════════════════════════════════════════════════════════════
# _live_arrival_fields — reiner Lücken-Füller
# ══════════════════════════════════════════════════════════════════════════════
def test_board_complete_wins_facts_are_not_consulted():
    """Hat das Board die Ankunft, bleibt sie unangetastet — die Fakten dürfen
    eine live gepollte Board-Ist-Zeit NIE überschreiben."""
    out = BP._live_arrival_fields(_BOARD_FULL, _FACTS_LH454)
    assert out['sched_arr'] == '2026-07-30T12:40:00+02:00'
    assert out['est_arr'] == '2026-07-30T12:52:00+02:00'
    assert out['arr_delay_min'] == 12
    assert out['dest_gate'] == 'B22'


def test_blind_board_is_filled_from_facts():
    """DER Owner-Fall: Board blind, Fakten kennen die Ankunft → sie kommt an."""
    out = BP._live_arrival_fields(_BOARD_BLIND, _FACTS_LH454)
    assert out['est_arr'] == '2026-07-30T12:36:00-07:00'
    assert out['sched_arr'] == '2026-07-30T12:40:00-07:00'
    assert out['arr_delay_min'] == -4
    assert out['dest_gate'] == 'G13'


def test_nothing_anywhere_stays_none():
    """KEIN Erfinden: kennt keine Quelle die Ankunft, bleibt jedes Feld None
    (der Client lässt die Zeile dann weg statt zu schätzen)."""
    out = BP._live_arrival_fields(_BOARD_BLIND, {})
    assert out == {'sched_arr': None, 'est_arr': None,
                   'arr_delay_min': None, 'dest_gate': None}


def test_none_inputs_do_not_raise():
    assert BP._live_arrival_fields(None, None) == {
        'sched_arr': None, 'est_arr': None,
        'arr_delay_min': None, 'dest_gate': None}


def test_partial_fill_sched_only():
    """Nur Fahrplan bekannt (Rang 2): sched_arr kommt, est_arr bleibt None —
    keine Ersatz-„Ist"-Zahl aus dem Fahrplan."""
    out = BP._live_arrival_fields(_BOARD_BLIND, {'sched_arr': '2026-07-30T12:40:00-07:00'})
    assert out['sched_arr'] == '2026-07-30T12:40:00-07:00'
    assert out['est_arr'] is None
    assert out['arr_delay_min'] is None


def test_board_est_kept_when_only_sched_missing():
    """Board hat die Ist-Zeit, nicht aber den Fahrplan → gemischt, jede Zahl
    von der Quelle, die sie wirklich hat."""
    board = dict(_BOARD_BLIND, esti_arr='2026-07-30T21:52:00+02:00')
    out = BP._live_arrival_fields(board, _FACTS_LH454)
    assert out['est_arr'] == '2026-07-30T21:52:00+02:00'   # Board bleibt
    assert out['sched_arr'] == '2026-07-30T12:40:00-07:00'  # Lücke gefüllt


def test_unknown_board_delay_is_not_published_as_zero():
    """`delay_known=False` ⇒ die Board-Zahl gilt nicht als Beleg. Sie wird
    verworfen, NICHT als 0 ausgeliefert — und die Fakten dürfen einspringen."""
    board = dict(_BOARD_BLIND, arr_delay_min=0, delay_known=False)
    assert BP._live_arrival_fields(board, {})['arr_delay_min'] is None
    assert BP._live_arrival_fields(board, _FACTS_LH454)['arr_delay_min'] == -4


# ══════════════════════════════════════════════════════════════════════════════
# _live_arrival_facts — Kosten-Gate
# ══════════════════════════════════════════════════════════════════════════════
def test_complete_board_skips_the_facts_read(monkeypatch):
    """Board-vollständig ⇒ `_flight_facts_from_obs` wird GAR NICHT gerufen
    (kein Supabase-Read, kein LH-Warmup) — der Normalfall Kurzstrecke zahlt
    für diesen Fix nichts."""
    calls = []
    monkeypatch.setattr(BP, '_flight_facts_from_obs',
                        lambda *a, **k: calls.append(k) or {})
    out = BP._live_arrival_facts('LH1550', '2026-07-30', 'FRA', 'MAD', _BOARD_FULL)
    assert calls == []
    assert out['est_arr'] == '2026-07-30T12:52:00+02:00'


def test_gap_triggers_facts_read_with_cached_only(monkeypatch):
    """Lücke ⇒ genau EIN Fakten-Read, und zwar zwingend mit
    `lh_cached_only=True`: das ist die Zusage „kein LH-Call pro Poll"."""
    calls = []

    def _facts(flight_no, date, dep_iata=None, arr_iata=None, lh_cached_only=False):
        calls.append((flight_no, date, dep_iata, arr_iata, lh_cached_only))
        return _FACTS_LH454

    monkeypatch.setattr(BP, '_flight_facts_from_obs', _facts)
    out = BP._live_arrival_facts('LH454', '2026-07-30', 'FRA', 'SFO', _BOARD_BLIND)
    assert calls == [('LH454', '2026-07-30', 'FRA', 'SFO', True)]
    assert out['est_arr'] == '2026-07-30T12:36:00-07:00'


def test_facts_read_failure_leaves_board_view_intact(monkeypatch):
    """Wirft die Fakten-Quelle, bleibt die Antwort exakt die Board-Sicht —
    der Poll darf an einem Fakten-Ausfall nie scheitern."""
    def _boom(*a, **k):
        raise RuntimeError('supabase down')

    monkeypatch.setattr(BP, '_flight_facts_from_obs', _boom)
    out = BP._live_arrival_facts('LH454', '2026-07-30', 'FRA', 'SFO', _BOARD_BLIND)
    assert out == {'sched_arr': None, 'est_arr': None,
                   'arr_delay_min': None, 'dest_gate': None}


def test_poll_path_never_makes_a_blocking_lh_call(monkeypatch):
    """Die Zusage bis ins LH-Modul durchgezogen (kein Vertrauen auf ein Mock
    eine Ebene darüber): `_live_arrival_facts` → `_flight_facts_from_obs` →
    `lh_flight_facts` MUSS mit cached_only=True ankommen. Nur so ist der
    Gateway-Call laut dessen Docstring unerreichbar (Memo-Hit oder {}).
    """
    seen = []

    def _fake_lh(fn, d, dep=None, arr=None, force=False, cached_only=False,
                 caller=None):
        seen.append({'flight': fn, 'cached_only': cached_only, 'caller': caller})
        return {}

    import blueprints.lh_open_api as LH
    monkeypatch.setattr(LH, 'lh_flight_facts', _fake_lh)
    monkeypatch.setattr(LH, 'is_lh_group', lambda fn: True)
    # Board-Read ausklammern — hier interessiert nur die LH-Kante.
    monkeypatch.setattr(BP, '_flight_facts_from_obs_uncached', lambda *a, **k: {})

    BP._live_arrival_facts('LH454', '2026-07-30', 'FRA', 'SFO', _BOARD_BLIND)

    assert seen, 'LH-Kante wurde gar nicht erreicht'
    assert all(c['cached_only'] is True for c in seen), seen


# ══════════════════════════════════════════════════════════════════════════════
# Route-Ebene: /api/ax/flight-live liefert die Ankunft wirklich aus
# ══════════════════════════════════════════════════════════════════════════════
def _live_pos():
    return {'lat': 57.3, 'lon': -110.4, 'alt': 36025, 'gs': 461.0,
            'track': 208.0, 'on_ground': False}


def _patch_route(monkeypatch, merged, facts):
    """flight-live so verdrahten, dass NUR die Ankunftsseite variiert."""
    monkeypatch.setattr(BP, '_life_app',
                        lambda name, default=None:
                        (lambda *a, **k: merged) if name == '_flight_obs_merged'
                        else default)
    monkeypatch.setattr(BP, '_machine_live',
                        lambda reg, want_route=True, targeted=False:
                        ('3c4b30', 'DLH454', _live_pos(),
                         {'src': 'FRA', 'dst': 'SFO', 'source': 'warehouse'}))
    monkeypatch.setattr(BP, '_flight_facts_from_obs', lambda *a, **k: facts)
    monkeypatch.setattr(BP, '_apply_paid_arrival_escalation',
                        lambda *a, **k: None)


def test_route_serves_arrival_from_facts_when_board_is_blind(client, monkeypatch):
    """Der behobene Owner-Fall, end-to-end über die Route."""
    _patch_route(monkeypatch, _BOARD_BLIND, _FACTS_LH454)
    r = client.get('/api/ax/flight-live/TESTTOKEN?flight_no=LH454'
                   '&date=2026-07-30&reg=D-ABYP&dep_iata=FRA&arr_iata=SFO')
    assert r.status_code == 200
    b = r.get_json()
    assert b['est_arr'] == '2026-07-30T12:36:00-07:00'
    assert b['sched_arr'] == '2026-07-30T12:40:00-07:00'
    assert b['arr_delay_min'] == -4
    assert b['dest_gate'] == 'G13'
    # Die Abflugseite bleibt exakt wie vorher (keine Kollateral-Änderung).
    assert b['est_dep'] == '2026-07-30T10:45:00+0200'
    assert b['dep_gate'] == 'Z66'


def test_route_keeps_none_when_no_source_knows_the_arrival(client, monkeypatch):
    """Ohne Quelle bleibt die Ankunft None — die Karte lässt die Zeile weg,
    statt eine Landung aus progress/Großkreis zu erfinden."""
    _patch_route(monkeypatch, _BOARD_BLIND, {})
    r = client.get('/api/ax/flight-live/TESTTOKEN?flight_no=LH454'
                   '&date=2026-07-30&reg=D-ABYP&dep_iata=FRA&arr_iata=SFO')
    assert r.status_code == 200
    b = r.get_json()
    assert b['est_arr'] is None
    assert b['sched_arr'] is None
    assert b['arr_delay_min'] is None
    assert b['dest_gate'] is None
