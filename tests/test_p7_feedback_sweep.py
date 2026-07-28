"""P7-Backend-Feedback-Sweep (2026-07-27) — Regressionstests für drei
User-Meldungen + den DST-Fix in pickup_utc_for_leg:

  1) BIRGIT — Geister-Pushes zu einem gestrichenen Umlauf: `ical_sectors` (und
     `legs`) fehlten in der Räum-Liste des Reconcile → der stornierte Tag
     überlebte als Zombie-Row, und der LH-MQTT-Fanout (_users_for_flight liest
     NUR raw_event->ical_sectors) pushte ewig weiter. End-to-End-Drei-Schritt:
     Reconcile → Save-Zustand → Fanout findet den User NICHT mehr.
  2) FLORIAN — Push-WHITELIST statt Blacklist: Push nur bei PU neu/geändert,
     erstem Abflug, Klasse, Routing/Legs, Layover-Ort, Tag neu/entfallen.
     Loch A (PU gelöscht + Blockzeiten gedriftet im selben Update) und
     Loch B (Tage ohne ical_sectors) sind explizit abgedeckt.
  3) KEVIN — Trip-Stats zählen den Flugbuch-Import mit (Commit 1e170dd hatte
     nur den Crew-Passport gefixt); Roster gewinnt bei Überlapp; der Merge
     mutiert keine Loader-Strukturen (cache-sicher).
  Z3) pickup_utc_for_leg rechnet jetzt in absoluter UTC-Differenz — an
     DST-Kanten war die Wanduhr-Arithmetik 1 h falsch und verwarf legitime
     Pickups still.
"""
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from unittest.mock import patch, MagicMock

import pytest

import app as A
from blueprints import lh_mqtt
from blueprints.crew_live_state import pickup_utc_for_leg


# ══════════════════════════════════════════════════════════════════════════════
# 1 · BIRGIT — Reconcile räumt ical_sectors; Fanout verstummt
# ══════════════════════════════════════════════════════════════════════════════

def _d(offset):
    return (datetime.now() + timedelta(days=offset)).strftime('%Y-%m-%d')


def _ev(datum, summary):
    return {'summary': summary, 'location': '', 'start': datum,
            'start_iso': datum + 'T10:00:00', '_multiday_dates': [datum]}


def _sector_for(datum, flight='LH441', frm='FRA', to='DTW'):
    return {'flight': flight, 'from': frm, 'to': to,
            'dep_iso': f'{datum}T09:00:00Z', 'arr_iso': f'{datum}T18:00:00Z'}


def _briefing_day(datum, flight='LH441'):
    return {'ical_summary': f'{flight} FRA-DTW', 'ical_location': 'FRA',
            'ical_start_iso': datum + 'T09:00:00',
            'ical_end_iso': datum + 'T18:00:00',
            'ical_sectors': [_sector_for(datum, flight)],
            'legs': [{'flight': flight, 'from': 'FRA', 'to': 'DTW'}]}


def _mqtt_rows(briefings):
    """Simuliert, was der LH-MQTT-Fanout aus Supabase liest: pro Tag die
    raw_event->ical_sectors der (nach dem Save) noch existierenden Rows."""
    rows = []
    for dkey, day in briefings.items():
        secs = (day or {}).get('ical_sectors') or []
        if secs:
            rows.append({'token': 'birgit', 'datum': dkey, 'sectors': secs})
    return rows


def test_birgit_dreischritt_reconcile_save_fanout_verstummt():
    d_cancel = _d(2)               # der gestrichene Umlauf-Tag (Zukunft)
    d_keep = _d(3)                 # neuer Reserve-Flug, bleibt im Feed
    briefings = {
        d_cancel: _briefing_day(d_cancel, 'LH441'),
        d_keep: _briefing_day(d_keep, 'LH500'),
    }
    # VORHER: der Fanout findet Birgit für LH441 am Topic-Datum.
    rows = _mqtt_rows(briefings)
    topic_date = lh_mqtt._sector_topic_dates(_sector_for(d_cancel))[0]
    assert lh_mqtt._users_for_flight(rows, 'LH', '441', topic_date), \
        'Setup kaputt: Fanout muss den User VOR dem Reconcile finden'
    # Feed-Update: d_cancel ist raus (aus der Reserve genommen), d_keep bleibt,
    # heute ist auch belegt (gesunder Feed bis in die Zukunft).
    feed = [_ev(_d(0), 'Standby'), _ev(d_keep, 'LH500 FRA-JFK')]
    A._reconcile_month_briefings('TESTTOKEN_NOSB', briefings, feed)
    # Der gestrichene Tag ist KOMPLETT weg (kein Sektoren-Zombie, den der
    # Upsert-Save wiederbeleben könnte) …
    assert d_cancel not in briefings, \
        'gestrichener Tag muss samt ical_sectors/legs verschwinden'
    # … und der Fanout auf den Nach-Save-Zustand findet niemanden mehr.
    assert lh_mqtt._users_for_flight(_mqtt_rows(briefings), 'LH', '441',
                                     topic_date) == []
    # Der behaltene Feed-Tag bleibt unangetastet.
    assert d_keep in briefings
    assert briefings[d_keep].get('ical_sectors')


def test_birgit_bestands_zombie_nur_sektoren_wird_geraeumt():
    # BESTANDSDATEN: ein früher halb-geprunter Tag (Summary weg, Sektoren-Rest
    # lebt). Das alte Clearing-Gate prüfte nur die Summary-Keys → Zombie blieb
    # für immer. Jetzt zählen ical_sectors/legs als Import-Beleg.
    d_zombie = _d(2)
    briefings = {d_zombie: {'ical_sectors': [_sector_for(d_zombie)],
                            'legs': [{'flight': 'LH441'}]}}
    feed = [_ev(_d(0), 'Standby'), _ev(_d(3), 'LH500 FRA-JFK')]
    A._reconcile_month_briefings('TESTTOKEN_NOSB', briefings, feed)
    assert d_zombie not in briefings, 'Bestands-Zombie muss nachgeräumt werden'


def test_birgit_tag_mit_nur_manuellen_daten_bleibt():
    # Ein Tag, dessen Briefing-Dict neben den iCal-Keys manuelle Daten trägt
    # (z. B. personal_note): die iCal-Reste werden geräumt, der Tag mit den
    # manuellen Daten BLEIBT.
    d_manual = _d(2)
    day = _briefing_day(d_manual)
    day['personal_note'] = 'Hotel-Tipp nicht vergessen'
    briefings = {d_manual: day}
    feed = [_ev(_d(0), 'Standby'), _ev(_d(3), 'LH500 FRA-JFK')]
    A._reconcile_month_briefings('TESTTOKEN_NOSB', briefings, feed)
    assert d_manual in briefings, 'Tag mit manuellen Daten darf nicht sterben'
    assert briefings[d_manual].get('personal_note') == 'Hotel-Tipp nicht vergessen'
    assert not briefings[d_manual].get('ical_sectors'), \
        'aber die Sektoren müssen raus (sonst pusht der Fanout weiter)'
    assert not briefings[d_manual].get('legs')


def test_birgit_user_ohne_sektoren_unveraendert():
    # User ohne ical_sectors (reine Summary-Tage): Verhalten wie bisher —
    # stale Tag verschwindet, Feed-Tage bleiben.
    d_stale, d_keep = _d(2), _d(3)
    briefings = {
        d_stale: {'ical_summary': 'Standby', 'ical_start_iso': d_stale + 'T08:00:00'},
        d_keep: {'ical_summary': 'LH500', 'ical_start_iso': d_keep + 'T08:00:00'},
    }
    feed = [_ev(_d(0), 'X'), _ev(d_keep, 'LH500')]
    A._reconcile_month_briefings('TESTTOKEN_NOSB', briefings, feed)
    assert d_stale not in briefings
    assert d_keep in briefings


# ══════════════════════════════════════════════════════════════════════════════
# 2 · FLORIAN — Push-Whitelist
# ══════════════════════════════════════════════════════════════════════════════

def _sec(flight='LH440', frm='FRA', to='IAH',
         dep='2026-08-02T08:00:00Z', arr='2026-08-02T18:30:00Z'):
    return {'flight': flight, 'from': frm, 'to': to,
            'dep_iso': dep, 'arr_iso': arr}


def _flug_tag(datum='2026-08-02', pickup='07:10', start='07:10', end='18:30',
              dep='2026-08-02T08:00:00Z', arr='2026-08-02T18:30:00Z',
              layover=''):
    day = {'datum': datum, 'klass': 'Flug', 'routing': 'FRA-IAH',
           'ical_sectors': [_sec(dep=dep, arr=arr)],
           'reader_facts': {'start_time': start, 'end_time': end,
                            'layover_ort': layover}}
    if pickup:
        day['pickup'] = pickup
    return day


def _mod(old, new, datum='2026-08-02'):
    return {'kind': 'modified', 'datum': datum, 'old': old, 'new': new}


def test_whitelist_loch_a_pu_geloescht_und_blockzeit_gedriftet_kein_push():
    # DER LH-Normalfall, der beide alten Gates gleichzeitig aushebelte:
    # PU weg + Ist-Zeiten (arr_iso/end_time) nachgetragen im selben Update.
    old = _flug_tag()
    new = _flug_tag(pickup='', start='07:10', end='18:47',
                    arr='2026-08-02T18:47:00Z')
    assert A._roster_change_is_push_worthy(_mod(old, new)) is False


def test_whitelist_reines_pu_loeschen_kein_push():
    old = _flug_tag()
    new = _flug_tag(pickup='')
    assert A._roster_change_is_push_worthy(_mod(old, new)) is False


def test_whitelist_pu_geaendert_pusht_praesenz_flip_nicht():
    # POLICY-NACHZUG 2026-07-28 (Owner-Fall 30.07.2026, AT-AA10DE7B8DBD45E7):
    # Der Push kam, weil der Marker „12:40 LT Pickup SFO" AUFTAUCHTE — bei
    # byte-identischem Leg LH455 SFO-FRA (dep 21:40Z). Das PRÄSENZ-Flippen der
    # PU ist die Signatur des Flip-Flops (fleet-weit oszillierten 51 von 52
    # mehrfach belegten Tag-Zellen A→B→A) und darf nicht mehr pushen.
    ohne = _flug_tag(pickup='')
    mit = _flug_tag(pickup='06:40')
    assert A._roster_change_is_push_worthy(_mod(ohne, mit)) is False
    assert A._roster_change_is_push_worthy(_mod(mit, ohne)) is False
    # Eine echte PU-VERSCHIEBUNG (beidseitig gefüllt, ≥ 5 min) bleibt eine
    # Verlauf-Änderung — GATE 4 (2026-07-28) hält sie aber vom Push fern:
    # dieselben Legs, dieselbe Route, nur eine andere Uhrzeit.
    frueher = _flug_tag(pickup='07:10')
    spaeter = _flug_tag(pickup='07:40')
    assert A._rc_meaningfully_modified(frueher, spaeter) is True
    assert A._roster_change_is_push_worthy(_mod(frueher, spaeter)) is False
    # Minuten-Korrektur darunter ist schon im Verlauf keine Änderung.
    assert A._roster_change_is_push_worthy(
        _mod(frueher, _flug_tag(pickup='07:13'))) is False


def test_whitelist_erster_abflug_im_verlauf_blockzeiten_nicht():
    old = _flug_tag()
    # Nur arr_iso/end_time gedriftet → weder Verlauf noch Push.
    drift = _flug_tag(end='18:52', arr='2026-08-02T18:52:00Z')
    assert A._rc_meaningfully_modified(old, drift) is False
    assert A._roster_change_is_push_worthy(_mod(old, drift)) is False
    # Erster Abflug um 75 min verschoben → Verlauf ja; der Push seit GATE 4
    # erst ab 3 h Verschiebung UND nur vor Dienstantritt.
    dep_shift = _flug_tag(dep='2026-08-02T09:15:00Z')
    assert A._rc_meaningfully_modified(old, dep_shift) is True
    assert A._roster_change_is_push_worthy(_mod(old, dep_shift)) is False


def test_whitelist_klasse_routing_layover_pushen():
    old = _flug_tag()
    # POLICY-NACHZUG 2026-07-28: ein klass-Wechsel bei UNVERÄNDERTEN Sektoren
    # ist kein Dienstwechsel mehr (live 107/219 'modified' kamen nur von klass,
    # 101 davon bei byte-identischen Legs). Erst wenn der Flug-Beleg wirklich
    # verschwindet, ist „Flug wird Standby" eine Änderung.
    klasse = dict(_flug_tag(), klass='Standby')
    assert A._roster_change_is_push_worthy(_mod(old, klasse)) is False
    echter_standby = {'datum': '2026-08-02', 'klass': 'Standby', 'routing': '',
                      'reader_facts': {'start_time': '07:10', 'end_time': '18:30'}}
    assert A._roster_change_is_push_worthy(_mod(old, echter_standby)) is True
    routing = dict(_flug_tag(), routing='FRA-JFK')
    assert A._roster_change_is_push_worthy(_mod(old, routing)) is True
    neuer_leg = _flug_tag()
    neuer_leg['ical_sectors'] = [_sec(), _sec(flight='LH441', frm='IAH', to='FRA')]
    assert A._roster_change_is_push_worthy(_mod(old, neuer_leg)) is True
    layover = _flug_tag(layover='JFK')
    alt_layover = _flug_tag(layover='BOS')
    assert A._roster_change_is_push_worthy(_mod(layover, alt_layover)) is True


def test_whitelist_loch_b_tag_ohne_sektoren():
    # Standby-/Ground-Tage ohne ical_sectors: end_time-Minutenpflege bleibt
    # jetzt still (Loch B), aber ein echter Standby-Beginn-Shift pusht.
    old = {'datum': '2026-08-02', 'klass': 'Standby', 'routing': '',
           'reader_facts': {'start_time': '08:00', 'end_time': '16:00'}}
    endpflege = {'datum': '2026-08-02', 'klass': 'Standby', 'routing': '',
                 'reader_facts': {'start_time': '08:00', 'end_time': '16:04'}}
    assert A._roster_change_is_push_worthy(_mod(old, endpflege)) is False
    # Der Standby-BEGINN-Shift landet im Verlauf; gepusht wird er seit GATE 4
    # nicht mehr — der Dienst selbst (Standby) ist unverändert.
    shift = {'datum': '2026-08-02', 'klass': 'Standby', 'routing': '',
             'reader_facts': {'start_time': '10:00', 'end_time': '18:00'}}
    assert A._rc_meaningfully_modified(old, shift) is True
    assert A._roster_change_is_push_worthy(_mod(old, shift)) is False


def test_whitelist_neuer_und_entfallener_tag_pushen():
    assert A._roster_change_is_push_worthy(
        {'kind': 'added', 'datum': '2026-08-02', 'new': _flug_tag()}) is True
    assert A._roster_change_is_push_worthy(
        {'kind': 'removed', 'datum': '2026-08-02', 'old': _flug_tag()}) is True


def test_whitelist_defensiv():
    assert A._roster_change_is_push_worthy({}) is True      # fail-open
    assert A._roster_change_is_push_worthy(None) is True


# ── Endpoint-Ebene: take_roster_snapshot nutzt die Whitelist ─────────────────

def _snapshot_env(tmp_path, old_tage):
    changes_file = tmp_path / 'roster_changes_test.json'
    push = MagicMock()
    return (
        patch.object(A, '_roster_snapshot_read',
                     return_value={'tage': old_tage} if old_tage else {}),
        patch.object(A, '_roster_snapshot_save', return_value=True),
        patch.object(A, '_roster_snapshot_path',
                     return_value=str(tmp_path / 'snap.json')),
        patch.object(A, '_roster_changes_path', return_value=str(changes_file)),
        patch.object(A, '_crew_flight_ingest', return_value=None),
        patch.object(A, '_push_notify_async', push),
        patch.object(A, '_profile_homebase_cached', return_value='FRA'),
        push,
        changes_file,
    )


def _post(tmp_path, old, new):
    p1, p2, p3, p4, p5, p6, p7, push, changes_file = _snapshot_env(tmp_path, old)
    with p1, p2, p3, p4, p5, p6, p7:
        client = A.app.test_client()
        r = client.post('/api/user/roster-snapshot/testtoken123',
                        json={'tage': new})
    return r, push, changes_file


def _future_flug_tag(days_ahead=3, **kw):
    d = (date.today() + timedelta(days=days_ahead)).isoformat()
    kw.setdefault('dep', f'{d}T08:00:00Z')
    kw.setdefault('arr', f'{d}T18:30:00Z')
    day = _flug_tag(datum=d, **kw)
    day['ical_sectors'] = [_sec(dep=kw['dep'], arr=kw['arr'])]
    return day


def test_endpoint_loch_a_weder_verlauf_noch_push(tmp_path):
    # Seit 2026-07-28 (Owner: „Verlauf zeigt immer wieder Änderungen") wird
    # Rauschen auch nicht mehr in den Verlauf geschrieben — Push-Gate und
    # Verlauf-Gate sind dieselbe Regel.
    old = _future_flug_tag()
    new = _future_flug_tag(pickup='', end='18:47')
    new['ical_sectors'][0]['arr_iso'] = new['ical_sectors'][0]['arr_iso'].replace('18:30', '18:47')
    r, push, changes_file = _post(tmp_path, old=[old], new=[new])
    assert r.status_code == 200
    assert r.get_json()['changes_count'] == 0
    assert json.loads(changes_file.read_text())['pending'] == []
    assert push.call_count == 0


def test_endpoint_erster_abflug_im_verlauf_ohne_push(tmp_path):
    # 75-min-Abflug-Shift: Verlauf ja, Push nein (GATE 4, ab 3 h und nur vor
    # Dienstantritt). Der 4-h-Fall steht in test_roster_push_gates.py.
    old = _future_flug_tag()
    new = _future_flug_tag()
    new['ical_sectors'][0]['dep_iso'] = new['ical_sectors'][0]['dep_iso'].replace('08:00', '09:15')
    r, push, changes_file = _post(tmp_path, old=[old], new=[new])
    assert r.status_code == 200
    assert r.get_json()['changes_count'] == 1     # in-App-Liste bleibt komplett
    assert len(json.loads(changes_file.read_text())['pending']) == 1
    assert push.call_count == 0


# ══════════════════════════════════════════════════════════════════════════════
# 3 · KEVIN — Trip-Stats zählen den Flugbuch-Import
# ══════════════════════════════════════════════════════════════════════════════

_IMPORT_LEGS = [
    # Historische Karriere-Legs (lange vor App-Nutzung), inkl. SYD-Fernstrecke.
    {'date': '2019-05-10', 'flight': 'LH792', 'from': 'FRA', 'to': 'SYD',
     'block_min': 1150, 'type': 'B747', 'reg': 'D-ABYT'},
    {'date': '2019-05-13', 'flight': 'LH793', 'from': 'SYD', 'to': 'FRA',
     'block_min': 1180, 'type': 'B747', 'reg': 'D-ABYT'},
    # Leg, das das Roster schon trägt → muss übersprungen werden.
    {'date': '2026-07-01', 'flight': 'LH400', 'from': 'FRA', 'to': 'JFK',
     'block_min': 500, 'type': 'B744'},
    # Kaputtes Leg (kein Datum) → still ignoriert.
    {'date': '', 'flight': 'LH999', 'from': 'FRA', 'to': 'JFK'},
]

_ROSTER_TAGE = [
    {'datum': '2026-07-01', 'klass': 'Z72', 'routing': 'FRA-JFK',
     'reader_facts': {'start_time': '08:00', 'end_time': '18:00'}},
]


def _trip_stats_with(monkeypatch, legs, tage):
    monkeypatch.setattr(A, '_logbook_import_load',
                        lambda tok: {'legs': legs} if legs else {})
    monkeypatch.setattr(A, '_roster_snapshot_read', lambda tok: {'tage': tage})
    monkeypatch.setattr(A, '_flight_ops_path', lambda tok: None)
    monkeypatch.setitem(A._store, 'kevin-test-token', {})
    return A._trip_stats_compute('kevin-test-token')


def test_kevin_import_zaehlt_in_lifetime(monkeypatch):
    out = _trip_stats_with(monkeypatch, _IMPORT_LEGS, _ROSTER_TAGE)
    life = out['lifetime']
    # Roster: 1 Leg (FRA-JFK) + Import: 2 neue Legs (SYD-Rotation); das
    # Überlapp-Leg vom 2026-07-01 zählt NICHT doppelt, das kaputte gar nicht.
    assert life['flights'] == 3
    assert life['distance_km'] > 30000            # 2× FRA-SYD ≈ 33.000 km
    assert 'AU' in life['countries_list']         # SYD bringt Australien
    assert life['top_aircraft'] == 'B747'
    assert out['has_data'] is True
    # YTD bleibt Roster-only (Import-Legs sind von 2019).
    assert out['ytd']['flights'] == 1
    # Flugstunden: Roster-Dienst 10h + Import-Blockzeit 2330min (die
    # 20-h-Sanity-Kappe pro Leg gilt wie im Passport-Merge).
    assert life['hours_flown'] == pytest.approx(10 + 2330 / 60.0, abs=0.1)


def test_kevin_user_ohne_import_unveraendert(monkeypatch):
    out = _trip_stats_with(monkeypatch, [], _ROSTER_TAGE)
    assert out['lifetime']['flights'] == 1
    assert out['ytd']['flights'] == 1


def test_kevin_nur_import_hat_daten(monkeypatch):
    out = _trip_stats_with(monkeypatch, _IMPORT_LEGS[:2], [])
    assert out['has_data'] is True
    assert out['lifetime']['flights'] == 2
    assert out['empty_reason'] is None


def test_kevin_merge_mutiert_loader_daten_nicht(monkeypatch):
    # Cache-Sicherheit: der Merge darf die vom Loader gelieferten Strukturen
    # nicht anfassen (Monotonie-Falle aus dem Passport-Merge-Review) — zweimal
    # rechnen muss identische Zahlen geben.
    legs = [dict(L) for L in _IMPORT_LEGS]
    frozen = json.dumps(legs, sort_keys=True)
    out1 = _trip_stats_with(monkeypatch, legs, _ROSTER_TAGE)
    out2 = _trip_stats_with(monkeypatch, legs, _ROSTER_TAGE)
    assert out1['lifetime']['flights'] == out2['lifetime']['flights']
    assert json.dumps(legs, sort_keys=True) == frozen, \
        'Loader-Struktur wurde in-place mutiert'


# ══════════════════════════════════════════════════════════════════════════════
# Z3 · pickup_utc_for_leg — DST-sauber (absolute UTC-Differenz)
# ══════════════════════════════════════════════════════════════════════════════

def test_pickup_dst_fruehjahr_legitimer_vorlauf_wird_akzeptiert():
    # Europe/Berlin 2026-03-29: 02:00 CET → 03:00 CEST (die Nacht ist 1 h kurz).
    # Abflug 08:50 CEST (= 06:50Z), Pickup 01:55 CET (= 00:55Z): realer Vorlauf
    # 5 h 55 → legitim. Die alte Wanduhr-Rechnung machte daraus 6 h 55 und
    # verwarf den Pickup still (return None).
    got = pickup_utc_for_leg((1, 55), '2026-03-29T06:50:00Z', 'Europe/Berlin')
    assert got is not None, 'legitimer Pickup über die DST-Lücke darf nicht sterben'
    assert got == datetime(2026, 3, 29, 0, 55, tzinfo=timezone.utc)


def test_pickup_dst_herbst_ambige_stunde_nimmt_spaetere_vorkommnis():
    # Europe/Berlin 2026-10-25: 03:00 CEST → 02:00 CET (02:xx existiert doppelt).
    # Abflug 08:00 CET (= 07:00Z), Pickup „02:30": 1. Vorkommnis 00:30Z (CEST,
    # Vorlauf 6 h 30 → unplausibel), 2. Vorkommnis 01:30Z (CET, Vorlauf 5 h 30).
    # fold=0 wählte stur die erste und lag 1 h daneben.
    got = pickup_utc_for_leg((2, 30), '2026-10-25T07:00:00Z', 'Europe/Berlin')
    assert got == datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)


def test_pickup_dst_herbst_echter_ueber_6h_vorlauf_wird_verworfen():
    # Abflug 07:30 CET (= 06:30Z), Pickup 01:45 CEST (= 25.10. 23:45Z am 24.):
    # absoluter Vorlauf 6 h 45 → raus. Die alte Wanduhr-Rechnung sah nur 5 h 45
    # und ließ ihn fälschlich durch.
    got = pickup_utc_for_leg((1, 45), '2026-10-25T06:30:00Z', 'Europe/Berlin')
    assert got is None


def test_pickup_normalfaelle_unveraendert():
    # Kein DST im Spiel: Verhalten wie vorher (Ortszeit + Mitternachts-Wrap).
    got = pickup_utc_for_leg((5, 30), '2026-07-15T06:00:00Z', 'Europe/Madrid')
    assert got == datetime(2026, 7, 15, 3, 30, tzinfo=timezone.utc)
    # Mitternachts-Wrap: Pickup 23:00 Vortag, Abflug 00:30 lokal.
    got = pickup_utc_for_leg((23, 0), '2026-07-15T22:30:00Z', 'Europe/Berlin')
    assert got == datetime(2026, 7, 15, 21, 0, tzinfo=timezone.utc)
    # Unplausibel (>6 h) bleibt None; kaputte TZ bleibt None.
    assert pickup_utc_for_leg((1, 0), '2026-07-15T12:00:00Z', 'Europe/Berlin') is None
    assert pickup_utc_for_leg((5, 30), '2026-07-15T06:00:00Z', None) is None


# ── P7-Review-Nachbesserungen (2026-07-27) ───────────────────────────────────

def test_whitelist_meldezeit_shift_ohne_pickup_nur_im_verlauf():
    # Review-Fund E1: Sektor-Tag OHNE Pickup-Quelle (Homebase-Report) — eine
    # vorgezogene Meldezeit bleibt eine Verlauf-Änderung. GATE 4 (2026-07-28,
    # Owner + Forum-Thread „Sa 08.08: Briefing 00:35 → 01:05") nimmt sie aus
    # dem Push: Legs, Route und Ziel sind identisch.
    old = _flug_tag(pickup='', start='07:10')
    new = _flug_tag(pickup='', start='06:30')
    assert A._rc_meaningfully_modified(old, new) is True
    assert A._roster_change_is_push_worthy(_mod(old, new)) is False
    # MIT unveränderter PU bleibt die PU die tragende Zeit → Briefing-Drift
    # ohne Abflug-Shift bleibt still.
    old_pu = _flug_tag(pickup='06:40', start='07:10')
    new_pu = _flug_tag(pickup='06:40', start='07:25')
    assert A._roster_change_is_push_worthy(_mod(old_pu, new_pu)) is False


def test_flugnummern_padding_ist_kein_leg_tausch():
    # „LH 0440" == „LH440": ein Quellen-Padding-Flip darf weder Diff noch
    # Push auslösen (Review-Fund: wäre seit der Whitelist push-wirksam).
    old = _flug_tag()
    new = _flug_tag()
    new['ical_sectors'] = [dict(new['ical_sectors'][0], flight='LH 0440')]
    assert A._rc_sector_structure(old) == A._rc_sector_structure(new)
    assert A._roster_change_is_push_worthy(_mod(old, new)) is False
    assert A._rc_meaningfully_modified(old, new) is False
    # Echter Flugnummern-Tausch bleibt ein Leg-Tausch.
    other = _flug_tag()
    other['ical_sectors'] = [dict(other['ical_sectors'][0], flight='LH441')]
    assert A._roster_change_is_push_worthy(_mod(old, other)) is True
