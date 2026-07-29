"""OPERATIVE Hangout-Filter statt Demografie (Owner 2026-07-29).

„Demografie-Filter sind vom Tisch (keine Profildaten, will der Owner nicht).
Stattdessen: Filter, die aus dem ROSTER beantwortbar sind."

Abgedeckt:
  * Normalisierung: `same_hotel` wird — wie „same airline" — beim ERSTELLEN
    gegen den Ersteller aufgelöst und eingefroren; ohne bekanntes Hotel fällt
    die Einschränkung weg statt unerfüllbar gespeichert zu werden.
  * Filter-Matrix inkl. FAIL-CLOSED (Viewer ohne die nötige Roster-Info sieht
    den eingeschränkten Hangout NICHT).
  * Auswertung gegen ORT + DATUM DES HANGOUTS, nicht gegen „heute/hier".
  * Legacy: Alt-Hangouts ohne `audience` bleiben unverändert für alle sichtbar.
  * `restricted` ⇒ weiterhin KEIN Geo-Push.

SICHERHEIT: kein echter SB-/APNs-Call — alle Loader sind gemockt.
"""
import os

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import sys
from unittest.mock import patch

import pytest

import app as A


@pytest.fixture(autouse=True)
def _pin_app():
    prev = sys.modules.get('app')
    sys.modules['app'] = A
    yield
    if prev is not None:
        sys.modules['app'] = prev


TODAY = '2026-07-29'
VIEWER = 'AT-VIEWER'
OWNER = 'AT-OWNER'
LH = {'name': 'Miguel', 'airline': 'Lufthansa', 'homebase': 'FRA'}

# Ein „hier, 2 Nächte, morgen frei, heute angekommen"-Viewer.
OPS_FULL = {'here': True, 'place': 'BKK', 'hotel': 'Novotel BKK', 'nights': 2,
            'free_tomorrow': True, 'arriving_today': True,
            'departing_tomorrow': False}
OPS_NONE = {'here': False, 'place': None, 'hotel': None, 'nights': 0,
            'free_tomorrow': None, 'arriving_today': None,
            'departing_tomorrow': None}


# ── Normalisierung / Einfrieren ─────────────────────────────────────────────

def test_same_hotel_wird_beim_erstellen_eingefroren():
    aud = A._hangout_audience_normalize({'same_hotel': True}, LH,
                                        owner_ops=OPS_FULL)
    assert aud['same_hotel'] is True
    assert aud['hotel'] == 'novotel bkk'          # Vergleichs-Normalform
    assert aud['hotel_label'] == 'Novotel BKK'    # Anzeige


def test_same_hotel_ohne_bekanntes_hotel_faellt_weg():
    """Kennt das Verzeichnis das Hotel nicht, wird keine unerfüllbare
    Einschränkung gespeichert (gleiche Regel wie „same airline" ohne Airline)."""
    aud = A._hangout_audience_normalize({'same_hotel': True}, LH,
                                        owner_ops=OPS_NONE)
    assert aud is None


def test_operative_flags_werden_uebernommen():
    aud = A._hangout_audience_normalize(
        {'free_tomorrow': True, 'min_nights': 3, 'arriving_today': True,
         'departing_tomorrow': True}, LH, owner_ops=OPS_FULL)
    assert aud['free_tomorrow'] is True
    assert aud['min_nights'] == 3
    assert aud['arriving_today'] is True
    assert aud['departing_tomorrow'] is True


def test_min_nights_unter_zwei_ist_keine_einschraenkung():
    """1 Nacht = jeder, der überhaupt da ist → kein Filter."""
    for v in (None, 0, 1, 'zwei', -5):
        aud = A._hangout_audience_normalize({'min_nights': v}, LH,
                                            owner_ops=OPS_FULL)
        assert aud is None, v


def test_min_nights_ist_gedeckelt():
    aud = A._hangout_audience_normalize({'min_nights': 999}, LH,
                                        owner_ops=OPS_FULL)
    assert aud['min_nights'] == A._CREW_OPS_MAX_NIGHTS


def test_nur_echtes_true_zaehlt_kein_truthy_string():
    """Ein Client kann keinen Filter aus Versehen per 'false'-String setzen."""
    aud = A._hangout_audience_normalize(
        {'free_tomorrow': 'false', 'same_hotel': 1}, LH, owner_ops=OPS_FULL)
    assert aud is None


def test_operative_filter_gelten_als_restricted():
    """Konsequenz: eingeschränkt ⇒ kein Geo-Push (gleiche Grundlage)."""
    for aud in ({'v': 1, 'same_hotel': True, 'hotel': 'x'},
                {'v': 1, 'free_tomorrow': True},
                {'v': 1, 'min_nights': 2},
                {'v': 1, 'arriving_today': True},
                {'v': 1, 'departing_tomorrow': True},
                {'v': 1, 'circle_id': 'c1'}):
        assert A._hangout_audience_is_restricted(aud) is True, aud
    # Vibes/Freitext sind weiterhin KEINE Einschränkung.
    assert A._hangout_audience_is_restricted(
        {'v': 1, 'vibes': ['bar'], 'note': 'kanu'}) is False


# ── Matching-Matrix ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('aud,ops,expect', [
    # same_hotel
    ({'v': 1, 'same_hotel': True, 'hotel': 'novotel bkk'}, OPS_FULL, True),
    ({'v': 1, 'same_hotel': True, 'hotel': 'marriott bkk'}, OPS_FULL, False),
    ({'v': 1, 'same_hotel': True, 'hotel': 'novotel bkk'}, OPS_NONE, False),
    # free_tomorrow — Tri-State: nur True zählt
    ({'v': 1, 'free_tomorrow': True}, OPS_FULL, True),
    ({'v': 1, 'free_tomorrow': True}, dict(OPS_FULL, free_tomorrow=False), False),
    ({'v': 1, 'free_tomorrow': True}, dict(OPS_FULL, free_tomorrow=None), False),
    # min_nights
    ({'v': 1, 'min_nights': 2}, OPS_FULL, True),
    ({'v': 1, 'min_nights': 3}, OPS_FULL, False),
    ({'v': 1, 'min_nights': 2}, OPS_NONE, False),
    # arriving / departing
    ({'v': 1, 'arriving_today': True}, OPS_FULL, True),
    ({'v': 1, 'arriving_today': True}, dict(OPS_FULL, arriving_today=None), False),
    ({'v': 1, 'departing_tomorrow': True}, OPS_FULL, False),
    ({'v': 1, 'departing_tomorrow': True},
     dict(OPS_FULL, departing_tomorrow=True), True),
])
def test_match_operativ(aud, ops, expect):
    assert A._hangout_audience_matches(aud, LH, viewer_ops=ops) is expect


def test_match_ohne_ops_ist_fail_closed():
    """Fehlen die Roster-Fakten ganz, sieht der Viewer den Hangout NICHT."""
    for aud in ({'v': 1, 'same_hotel': True, 'hotel': 'novotel bkk'},
                {'v': 1, 'free_tomorrow': True},
                {'v': 1, 'min_nights': 2},
                {'v': 1, 'arriving_today': True},
                {'v': 1, 'departing_tomorrow': True}):
        assert A._hangout_audience_matches(aud, LH) is False, aud


def test_match_kombiniert_profil_und_operativ():
    aud = {'v': 1, 'airline': 'LUFTHANSA', 'free_tomorrow': True}
    assert A._hangout_audience_matches(aud, LH, viewer_ops=OPS_FULL) is True
    assert A._hangout_audience_matches(
        aud, {'airline': 'SWISS'}, viewer_ops=OPS_FULL) is False
    assert A._hangout_audience_matches(
        aud, LH, viewer_ops=dict(OPS_FULL, free_tomorrow=False)) is False


def test_label_nennt_die_operativen_filter():
    assert A._hangout_audience_label(
        {'v': 1, 'same_hotel': True, 'hotel': 'x'}) == 'Selbes Hotel'
    assert A._hangout_audience_label(
        {'v': 1, 'free_tomorrow': True, 'min_nights': 2}) == 'Morgen frei · ≥2 Nächte'
    assert A._hangout_audience_label(
        {'v': 1, 'circle_id': 'c1', 'circle_name': 'Deutschsprachig'}
    ) == 'Kreis Deutschsprachig'


# ── Row-Matching: Ort + Datum DES HANGOUTS ──────────────────────────────────

def _row(pid='h1', owner=OWNER, audience=None, iata='BKK', date=None):
    row = {'id': pid, 'user_token': owner, 'iata_code': iata,
           'lat': 13.6, 'lng': 100.7, 'pin_date': date, 'note': 'Bier?'}
    if audience is not None:
        row['audience'] = audience
    return row


def _ctx(ops_by_key=None, circles=()):
    """Kontext-Attrappe mit derselben Form wie _hangout_viewer_ctx."""
    ops_by_key = ops_by_key or {}
    seen = []

    def ops(iata, ref):
        seen.append((iata, ref))
        return ops_by_key.get((iata, ref), OPS_NONE)

    return {'profile': lambda: LH, 'ops': ops,
            'circles': lambda: set(circles)}, seen


def test_row_match_fragt_ort_und_datum_des_hangouts():
    ctx, seen = _ctx({('BKK', '2026-08-02'): OPS_FULL})
    row = _row(audience={'v': 1, 'free_tomorrow': True}, iata='BKK',
               date='2026-08-02')
    assert A._hangout_row_matches(row, LH, ctx) is True
    assert seen == [('BKK', '2026-08-02')]


def test_row_match_falscher_ort_trifft_nicht():
    ctx, _ = _ctx({('BKK', '2026-08-02'): OPS_FULL})
    row = _row(audience={'v': 1, 'free_tomorrow': True}, iata='JFK',
               date='2026-08-02')
    assert A._hangout_row_matches(row, LH, ctx) is False


def test_row_match_ohne_datum_nimmt_heute():
    ctx, seen = _ctx({('BKK', TODAY): OPS_FULL})
    row = _row(audience={'v': 1, 'min_nights': 2}, iata='BKK', date=None)
    with patch.object(A, '_crew_ops_today', return_value=TODAY):
        assert A._hangout_row_matches(row, LH, ctx) is True
    assert seen == [('BKK', TODAY)]


def test_row_match_ohne_kontext_fail_closed():
    row = _row(audience={'v': 1, 'free_tomorrow': True})
    assert A._hangout_row_matches(row, LH, None) is False


def test_row_match_legacy_ohne_audience_immer_sichtbar():
    assert A._hangout_row_matches(_row(), LH, None) is True
    assert A._hangout_row_matches(_row(audience={'v': 1, 'note': 'kanu'}),
                                 LH, None) is True


def test_row_match_reine_profilfilter_brauchen_keinen_kontext():
    """Der v1-Pfad bleibt exakt wie er war — kein zusätzlicher Read."""
    aud = {'v': 1, 'airline': 'LUFTHANSA'}
    assert A._hangout_row_matches(_row(audience=aud), LH, None) is True
    assert A._hangout_row_matches(_row(audience=aud), {'airline': 'SWISS'},
                                 None) is False


# ── Endpoint /api/user/hangouts ─────────────────────────────────────────────

def _list_hangouts(pins, ops_by_key=None, circles=(), token=VIEWER,
                   my_days=None):
    days = my_days if my_days is not None else {}

    def _fake_ops(tok, iata, ref=None, profile=None, days=None):
        return (ops_by_key or {}).get((iata, ref), OPS_NONE)

    with patch.object(A, '_hangouts_load_all_active', return_value=pins), \
         patch.object(A, '_friends_load', return_value={'friends': []}), \
         patch.object(A, '_profiles_load_bulk', return_value={}), \
         patch.object(A, '_profile_load', return_value={'profile': LH}), \
         patch.object(A, '_crew_ops_today', return_value=TODAY), \
         patch.object(A, '_crew_roster_days', return_value=days), \
         patch.object(A, '_crew_ops_facts', side_effect=_fake_ops), \
         patch.object(A, '_circles_of_user', return_value=set(circles)), \
         A.app.test_request_context(f'/api/user/hangouts/{token}'):
        return A.list_hangouts(token).get_json()


def test_endpoint_operativ_passender_viewer_sieht_hangout():
    data = _list_hangouts([_row('frei', audience={'v': 1, 'free_tomorrow': True})],
                          ops_by_key={('BKK', TODAY): OPS_FULL})
    assert [h['id'] for h in data['hangouts']] == ['frei']
    assert data['hangouts'][0]['audience_label'] == 'Morgen frei'


def test_endpoint_operativ_nicht_passender_viewer_bekommt_nichts():
    data = _list_hangouts([_row('frei', audience={'v': 1, 'free_tomorrow': True})],
                          ops_by_key={('BKK', TODAY): dict(OPS_FULL,
                                                           free_tomorrow=False)})
    assert data['hangouts'] == []


def test_endpoint_operativ_fail_closed_ohne_roster():
    """Viewer ohne Roster-Info → eingeschränkter Hangout wird gar nicht erst
    ausgeliefert; der offene daneben schon."""
    data = _list_hangouts([_row('eng', audience={'v': 1, 'min_nights': 2}),
                           _row('offen')])
    assert [h['id'] for h in data['hangouts']] == ['offen']


def test_endpoint_ersteller_sieht_eigenen_operativen_hangout_immer():
    data = _list_hangouts(
        [_row('meiner', owner=VIEWER, audience={'v': 1, 'same_hotel': True,
                                                'hotel': 'novotel bkk'})])
    assert [h['id'] for h in data['hangouts']] == ['meiner']
    assert data['hangouts'][0]['mine'] is True


def test_endpoint_kreis_nur_fuer_mitglieder():
    aud = {'v': 1, 'circle_id': 'c1', 'circle_name': 'Deutschsprachig'}
    assert _list_hangouts([_row('k', audience=aud)],
                          circles=['c1'])['hangouts'] != []
    assert _list_hangouts([_row('k', audience=aud)],
                          circles=['c2'])['hangouts'] == []
    assert _list_hangouts([_row('k', audience=aud)])['hangouts'] == []


def test_endpoint_legacy_bleibt_fuer_alle_sichtbar():
    data = _list_hangouts([_row('legacy'), _row('vibes',
                                                audience={'v': 1,
                                                          'vibes': ['bar']})])
    assert sorted(h['id'] for h in data['hangouts']) == ['legacy', 'vibes']


# ── Zweiter Auslieferungsweg: crew-at-destination ───────────────────────────

def _crew_at_destination(pins, ops_by_key=None, circles=(), token=VIEWER):
    def _fake_ops(tok, iata, ref=None, profile=None, days=None):
        return (ops_by_key or {}).get((iata, ref), OPS_NONE)

    with patch.object(A, '_user_future_layovers', return_value=[]), \
         patch.object(A, '_friends_load', return_value={'friends': []}), \
         patch.object(A, '_profiles_load_bulk', return_value={}), \
         patch.object(A, '_profile_load', return_value={'profile': LH}), \
         patch.object(A, '_user_current_iata', return_value='BKK'), \
         patch.object(A, '_manual_pins_load', return_value=[]), \
         patch.object(A, '_manual_pins_for_friends', return_value=[]), \
         patch.object(A, '_public_pins_at_iatas', return_value=pins), \
         patch.object(A, '_crew_ops_today', return_value=TODAY), \
         patch.object(A, '_crew_roster_days', return_value={}), \
         patch.object(A, '_crew_ops_facts', side_effect=_fake_ops), \
         patch.object(A, '_circles_of_user', return_value=set(circles)), \
         A.app.test_request_context(f'/api/user/crew-at-destination/{token}'):
        return A.get_crew_at_destination(token).get_json()


def test_crew_at_destination_filtert_operativ_ebenso():
    """Sonst käme ein eingeschränkter Hangout über die Hintertür doch an."""
    pins = [_row('eng', audience={'v': 1, 'free_tomorrow': True}),
            _row('offen')]
    ids = [p['id'] for p in _crew_at_destination(pins)['manual_pins']]
    assert ids == ['offen']
    ids = [p['id'] for p in _crew_at_destination(
        pins, ops_by_key={('BKK', TODAY): OPS_FULL})['manual_pins']]
    assert sorted(ids) == ['eng', 'offen']


# ── Erstellen: Snapshot + kein Geo-Push für Eingeschränkte ──────────────────

class _FakeInsert:
    def __init__(self, box):
        self.box = box

    def insert(self, row):
        self.box.append(row)
        return self

    def execute(self):
        return self


def _create(body, ops=None, circles=()):
    box = []

    class _SB:
        @staticmethod
        def table(name):
            assert name == 'manual_pins'
            return _FakeInsert(box)

    def _fake_ops(tok, iata, ref=None, profile=None, days=None):
        return ops if ops is not None else OPS_NONE

    with patch.object(A, 'SB_AVAILABLE', True), \
         patch.object(A, 'sb', _SB()), \
         patch.object(A, '_profile_load', return_value={'profile': LH}), \
         patch.object(A, '_crew_ops_facts', side_effect=_fake_ops), \
         patch.object(A, '_circles_of_user', return_value=set(circles)), \
         patch.object(A, '_circle_name_of', return_value='Deutschsprachig'), \
         patch.object(A, '_hangout_notify_nearby') as notify, \
         A.app.test_request_context(f'/api/user/manual-pins/{OWNER}',
                                    json=body, method='POST'):
        resp = A.create_manual_pin(OWNER)
    payload = resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()
    return payload, box, notify


def test_create_friert_das_hotel_des_erstellers_ein():
    payload, box, _ = _create(
        {'iata': 'BKK', 'date': '2026-08-02', 'audience': {'same_hotel': True}},
        ops=OPS_FULL)
    assert box[0]['audience']['hotel'] == 'novotel bkk'
    assert payload['pin']['audience_label'] == 'Selbes Hotel'


def test_create_operativ_eingeschraenkt_pusht_nicht():
    """Der Geo-Fanout kennt die Zielgruppe nicht — lieber kein Push."""
    _, _, notify = _create({'iata': 'BKK', 'audience': {'free_tomorrow': True}},
                           ops=OPS_FULL)
    notify.assert_not_called()


def test_create_kreis_nur_wenn_ersteller_mitglied_ist():
    payload, box, notify = _create(
        {'iata': 'BKK', 'audience': {'circle_id': 'c1'}}, circles=['c1'])
    assert box[0]['audience']['circle_id'] == 'c1'
    assert box[0]['audience']['circle_name'] == 'Deutschsprachig'
    notify.assert_not_called()

    payload, box, notify = _create(
        {'iata': 'BKK', 'audience': {'circle_id': 'fremd'}}, circles=['c1'])
    assert 'audience' not in box[0]
    assert payload['pin']['audience'] is None
    notify.assert_called_once()   # offen → Geo-Push wie gehabt


def test_create_offen_pusht_weiterhin():
    _, _, notify = _create({'iata': 'BKK'})
    notify.assert_called_once()
