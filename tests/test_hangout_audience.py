"""Hangout-Zielgruppe (Owner-Wunsch 2026-07-28).

„Was mich abhält, einen Hangout zu posten: ich kann nicht wählen, FÜR WEN er
ist." v1 filtert über die Profil-Fakten, die es HEUTE gibt: airline, homebase,
position (→ Cockpit/Kabine). Diese Tests decken ab:

  * Normalisierung beim Erstellen („same" → eingefrorener Wert)
  * Matching inkl. FAIL-CLOSED bei fehlendem Viewer-Fakt
  * Alt-Hangouts ohne `audience` bleiben für alle sichtbar
  * die beiden Auslieferungswege (/api/user/hangouts + crew-at-destination)
    liefern einen nicht passenden Hangout GAR NICHT erst aus
  * der Ersteller sieht seinen eigenen Hangout immer
  * eingeschränkte Hangouts lösen keinen Geo-Push aus

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


LH_FRA_CABIN = {'airline': 'Lufthansa', 'homebase': 'FRA', 'position': 'PU'}
LH_MUC_COCKPIT = {'airline': 'Lufthansa', 'homebase': 'MUC', 'position': 'FO'}
LX_ZRH_CABIN = {'airline': 'SWISS', 'homebase': 'ZRH', 'position': 'FB'}
EMPTY_PROFILE = {}


# ── Normalisierung ──────────────────────────────────────────────────────────

def test_normalize_same_airline_wird_eingefroren():
    aud = A._hangout_audience_normalize(
        {'airline': 'same', 'base': 'any'}, LH_FRA_CABIN)
    assert aud['airline'] == 'LUFTHANSA'
    assert aud['airline_label'] == 'Lufthansa'
    assert 'base' not in aud


def test_normalize_same_base():
    aud = A._hangout_audience_normalize({'base': 'same'}, LH_FRA_CABIN)
    assert aud['base'] == 'FRA'


# ── KONKRETE Base/Airline (v3, Owner 2026-07-29: „base münchen etc etc") ────

def test_normalize_konkrete_base_auch_ohne_eigene_base():
    """„Base MUC" behauptet nichts über den Ersteller — es braucht seinen
    Profil-Fakt also nicht (anders als „same")."""
    aud = A._hangout_audience_normalize({'base': 'muc'}, EMPTY_PROFILE)
    assert aud['base'] == 'MUC'


def test_normalize_konkrete_base_schlaegt_nicht_die_eigene():
    aud = A._hangout_audience_normalize({'base': 'MUC'}, LH_FRA_CABIN)
    assert aud['base'] == 'MUC'


def test_normalize_base_freitext_wird_verworfen():
    """„München" würde gegen die IATA-Homebase nie matchen → lieber gar keine
    Einschränkung als eine, die niemanden durchlässt."""
    assert A._hangout_audience_normalize({'base': 'München'},
                                         LH_FRA_CABIN) is None
    assert A._hangout_audience_normalize({'base': 'FRAN'}, LH_FRA_CABIN) is None


def test_normalize_base_any_bleibt_offen():
    assert A._hangout_audience_normalize({'base': 'any'}, LH_FRA_CABIN) is None


def test_normalize_konkrete_airline_wird_kanonisiert():
    """GEÄNDERT 2026-07-29 (Lufthansa Cargo): das Label benennt jetzt den
    Kreis, der WIRKLICH gefiltert wird, statt den Roh-String durchzureichen.
    „LX" filtert auf den SWISS-Bucket → „Nur SWISS" (vorher „Nur LX").
    Auslöser war der Cargo-Fall: „Nur Lufthansa Cargo" hätte eine Trennung
    versprochen, die der Filter gar nicht macht (er lässt die ganze LH-Crew
    durch — genau so gewollt, aber das Label darf nicht lügen).
    KEINE sichtbare Änderung für die App: jeder Wert aus dem iOS-Picker
    (`HangoutAudienceOptions.airlines`) ist selbst schon der Bucket-Name und
    behält deshalb seine Schreibweise — siehe die Fälle unten."""
    aud = A._hangout_audience_normalize({'airline': 'LX'}, LH_FRA_CABIN)
    assert aud['airline'] == 'SWISS'
    assert aud['airline_label'] == 'SWISS'
    # Schreibweise des Users bleibt, wo der Roh-String selbst der Bucket ist.
    for name in ('Lufthansa', 'Lufthansa City', 'Eurowings', 'Discover',
                 'Condor', 'Edelweiss', 'Austrian', 'Swiss', 'Brussels',
                 'TUIfly', 'ITA Airways'):
        aud = A._hangout_audience_normalize({'airline': name}, LH_FRA_CABIN)
        assert aud['airline_label'] == name, name


def test_normalize_konkrete_airline_cargo_meint_ganz_lufthansa():
    """Cargo ist operativ Lufthansa: Zielgruppe und Label sagen das auch."""
    aud = A._hangout_audience_normalize({'airline': 'Lufthansa Cargo'},
                                        LH_FRA_CABIN)
    assert aud['airline'] == 'LUFTHANSA'
    assert aud['airline_label'] == 'Lufthansa'
    assert A._hangout_audience_label(aud).startswith('Nur Lufthansa')


def test_konkrete_base_filtert_wie_die_eigene():
    aud = A._hangout_audience_normalize({'base': 'MUC'}, LH_FRA_CABIN)
    assert A._hangout_audience_matches(aud, LH_MUC_COCKPIT) is True
    assert A._hangout_audience_matches(aud, LH_FRA_CABIN) is False
    # FAIL-CLOSED: ohne eigene Base im Profil sieht man den Hangout nicht.
    assert A._hangout_audience_matches(aud, EMPTY_PROFILE) is False


def test_altbestand_mit_same_herkunft_bleibt_lesbar():
    """Abwärtskompatibilität: gespeichert wurde schon immer der AUFGELÖSTE
    Wert — die v3-Erweiterung ändert daran nichts."""
    alt = {'v': 1, 'base': 'FRA', 'airline': 'LUFTHANSA'}
    assert A._hangout_audience_matches(alt, LH_FRA_CABIN) is True
    assert A._hangout_audience_label(alt) == 'Nur Lufthansa · Base FRA'


def test_normalize_ohne_ersteller_fakt_faellt_einschraenkung_weg():
    """Wer selbst keine Airline im Profil hat, kann nicht darauf einschränken."""
    aud = A._hangout_audience_normalize(
        {'airline': 'same', 'base': 'same'}, EMPTY_PROFILE)
    assert aud is None


def test_normalize_beide_rollen_ist_keine_einschraenkung():
    aud = A._hangout_audience_normalize(
        {'roles': ['cockpit', 'cabin']}, LH_FRA_CABIN)
    assert aud is None


def test_normalize_freitext_alleine_schraenkt_nicht_ein():
    aud = A._hangout_audience_normalize(
        {'note': 'sportlich, Lust auf Kanu'}, LH_FRA_CABIN)
    assert aud['note'] == 'sportlich, Lust auf Kanu'
    assert A._hangout_audience_is_restricted(aud) is False


def test_normalize_ignoriert_muell():
    assert A._hangout_audience_normalize(None, LH_FRA_CABIN) is None
    assert A._hangout_audience_normalize('nope', LH_FRA_CABIN) is None
    assert A._hangout_audience_normalize(
        {'roles': 'cockpit'}, LH_FRA_CABIN) is None   # kein list → ignoriert


def test_normalize_unbekannte_keys_werden_verworfen_nicht_uebernommen():
    """Erweiterungspunkt: neue Filter brauchen bewusst Code hier — ein Client
    kann keinen unbekannten Filter-Key ins Storage schmuggeln."""
    aud = A._hangout_audience_normalize(
        {'airline': 'same', 'gender': 'f', 'max_age': 30}, LH_FRA_CABIN)
    assert set(aud) == {'v', 'airline', 'airline_label'}


# ── Rollen-Ableitung aus der Position ───────────────────────────────────────

@pytest.mark.parametrize('pos,expect', [
    ('CPT', 'cockpit'), ('FO', 'cockpit'), ('SF', 'cockpit'),
    ('Captain', 'cockpit'), ('First Officer', 'cockpit'),
    ('FA', 'cabin'), ('PU', 'cabin'), ('SEN', 'cabin'),
    ('P1', 'cabin'), ('Flugbegleiterin', 'cabin'), ('Purser', 'cabin'),
    ('', None), (None, None), ('Bodenpersonal', None),
])
def test_rolle_aus_position(pos, expect):
    assert A._hangout_role_of_position(pos) == expect


# ── Matching ────────────────────────────────────────────────────────────────

def test_match_ohne_audience_sehen_alle():
    assert A._hangout_audience_matches(None, EMPTY_PROFILE) is True
    assert A._hangout_audience_matches({}, EMPTY_PROFILE) is True
    assert A._hangout_audience_matches({'v': 1, 'note': 'egal'},
                                       EMPTY_PROFILE) is True


def test_match_airline():
    aud = {'v': 1, 'airline': 'LUFTHANSA'}
    assert A._hangout_audience_matches(aud, LH_FRA_CABIN) is True
    assert A._hangout_audience_matches(aud, LX_ZRH_CABIN) is False
    # Kanonisierung: „LH"/„DLH" ist dieselbe Airline wie „Lufthansa".
    assert A._hangout_audience_matches(aud, {'airline': 'DLH'}) is True


def test_match_base():
    aud = {'v': 1, 'base': 'FRA'}
    assert A._hangout_audience_matches(aud, LH_FRA_CABIN) is True
    assert A._hangout_audience_matches(aud, LH_MUC_COCKPIT) is False


def test_match_rolle():
    cockpit = {'v': 1, 'roles': ['cockpit']}
    cabin = {'v': 1, 'roles': ['cabin']}
    assert A._hangout_audience_matches(cockpit, LH_MUC_COCKPIT) is True
    assert A._hangout_audience_matches(cockpit, LH_FRA_CABIN) is False
    assert A._hangout_audience_matches(cabin, LH_FRA_CABIN) is True
    assert A._hangout_audience_matches(cabin, LH_MUC_COCKPIT) is False


def test_match_fail_closed_bei_fehlendem_viewer_fakt():
    """Fehlt dem Viewer der Fakt, gilt er als NICHT passend — nie als passend."""
    assert A._hangout_audience_matches({'v': 1, 'airline': 'LUFTHANSA'},
                                       EMPTY_PROFILE) is False
    assert A._hangout_audience_matches({'v': 1, 'base': 'FRA'},
                                       EMPTY_PROFILE) is False
    assert A._hangout_audience_matches({'v': 1, 'roles': ['cabin']},
                                       EMPTY_PROFILE) is False
    # Unverständliche Position ist NICHT automatisch Kabine.
    assert A._hangout_audience_matches(
        {'v': 1, 'roles': ['cabin']}, {'position': 'Bodenpersonal'}) is False


def test_match_alle_bedingungen_muessen_stimmen():
    aud = {'v': 1, 'airline': 'LUFTHANSA', 'base': 'FRA', 'roles': ['cabin']}
    assert A._hangout_audience_matches(aud, LH_FRA_CABIN) is True
    assert A._hangout_audience_matches(
        aud, dict(LH_FRA_CABIN, homebase='MUC')) is False


def test_audience_of_row_tolerant():
    assert A._hangout_audience_of_row({'audience': {'v': 1}}) == {'v': 1}
    assert A._hangout_audience_of_row({'audience': '{"v":1}'}) == {'v': 1}
    assert A._hangout_audience_of_row({'audience': 'kaputt'}) is None
    assert A._hangout_audience_of_row({'audience': None}) is None
    assert A._hangout_audience_of_row({}) is None          # Spalte fehlt
    assert A._hangout_audience_of_row(None) is None


def test_label():
    assert A._hangout_audience_label(
        {'v': 1, 'airline': 'LUFTHANSA', 'airline_label': 'Lufthansa',
         'base': 'FRA'}) == 'Nur Lufthansa · Base FRA'
    assert A._hangout_audience_label(
        {'v': 1, 'roles': ['cockpit']}) == 'Cockpit'
    assert A._hangout_audience_label({'v': 1, 'note': 'kanu'}) is None
    assert A._hangout_audience_label(None) is None


# ── Endpoint: /api/user/hangouts ────────────────────────────────────────────

VIEWER = 'viewer-token'
OWNER = 'owner-token'


def _pin(pid, owner=OWNER, audience=None, iata='FRA'):
    row = {'id': pid, 'user_token': owner, 'iata_code': iata,
           'lat': 50.0, 'lng': 8.5, 'pin_date': None, 'note': 'Bier?'}
    if audience is not None:
        row['audience'] = audience
    return row


def _list_hangouts(pins, viewer_profile, token=VIEWER):
    with patch.object(A, '_hangouts_load_all_active', return_value=pins), \
         patch.object(A, '_friends_load', return_value={'friends': []}), \
         patch.object(A, '_profiles_load_bulk', return_value={}), \
         patch.object(A, '_profile_load',
                      return_value={'profile': viewer_profile}), \
         A.app.test_request_context(f'/api/user/hangouts/{token}'):
        return A.list_hangouts(token).get_json()


def test_endpoint_legacy_hangout_ohne_audience_fuer_alle_sichtbar():
    data = _list_hangouts([_pin('legacy')], EMPTY_PROFILE)
    assert [h['id'] for h in data['hangouts']] == ['legacy']
    assert data['hangouts'][0]['audience'] is None
    assert data['hangouts'][0]['audience_label'] is None


def test_endpoint_passender_viewer_sieht_hangout():
    aud = {'v': 1, 'airline': 'LUFTHANSA', 'airline_label': 'Lufthansa',
           'base': 'FRA'}
    data = _list_hangouts([_pin('lh', audience=aud)], LH_FRA_CABIN)
    assert [h['id'] for h in data['hangouts']] == ['lh']
    assert data['hangouts'][0]['audience_label'] == 'Nur Lufthansa · Base FRA'


def test_endpoint_nicht_passender_viewer_bekommt_hangout_gar_nicht():
    aud = {'v': 1, 'airline': 'LUFTHANSA'}
    data = _list_hangouts([_pin('lh', audience=aud)], LX_ZRH_CABIN)
    assert data['hangouts'] == []


def test_endpoint_viewer_ohne_profilfakten_fail_closed():
    aud = {'v': 1, 'base': 'FRA'}
    data = _list_hangouts([_pin('restricted', audience=aud),
                           _pin('offen')], EMPTY_PROFILE)
    assert [h['id'] for h in data['hangouts']] == ['offen']


def test_endpoint_ersteller_sieht_eigenen_hangout_immer():
    """Auch wenn er selbst die eigene Zielgruppe nicht (mehr) trifft."""
    aud = {'v': 1, 'airline': 'LUFTHANSA', 'base': 'FRA'}
    data = _list_hangouts([_pin('meiner', owner=VIEWER, audience=aud)],
                          EMPTY_PROFILE)
    assert [h['id'] for h in data['hangouts']] == ['meiner']
    assert data['hangouts'][0]['mine'] is True


def test_endpoint_rollen_filter():
    aud = {'v': 1, 'roles': ['cockpit']}
    assert _list_hangouts([_pin('c', audience=aud)],
                          LH_MUC_COCKPIT)['hangouts'] != []
    assert _list_hangouts([_pin('c', audience=aud)],
                          LH_FRA_CABIN)['hangouts'] == []


# ── Zweiter Auslieferungsweg: crew-at-destination ───────────────────────────

def _crew_at_destination(pins, viewer_profile, token=VIEWER):
    with patch.object(A, '_user_future_layovers', return_value=[]), \
         patch.object(A, '_friends_load', return_value={'friends': []}), \
         patch.object(A, '_profiles_load_bulk', return_value={}), \
         patch.object(A, '_profile_load',
                      return_value={'profile': viewer_profile}), \
         patch.object(A, '_user_current_iata', return_value='FRA'), \
         patch.object(A, '_manual_pins_load', return_value=[]), \
         patch.object(A, '_manual_pins_for_friends', return_value=[]), \
         patch.object(A, '_public_pins_at_iatas', return_value=pins), \
         A.app.test_request_context(f'/api/user/crew-at-destination/{token}'):
        return A.get_crew_at_destination(token).get_json()


def test_crew_at_destination_filtert_ebenfalls():
    aud = {'v': 1, 'airline': 'LUFTHANSA'}
    pins = [_pin('lh', audience=aud), _pin('offen')]
    ids = [p['id'] for p in
           _crew_at_destination(pins, LX_ZRH_CABIN)['manual_pins']]
    assert ids == ['offen']
    ids = [p['id'] for p in
           _crew_at_destination(pins, LH_FRA_CABIN)['manual_pins']]
    assert sorted(ids) == ['lh', 'offen']


# ── Erstellen ───────────────────────────────────────────────────────────────

class _FakeInsert:
    def __init__(self, box, fail_with=None):
        self.box = box
        self.fail_with = fail_with

    def insert(self, row):
        self.box.append(row)
        if self.fail_with:
            raise Exception(self.fail_with)
        return self

    def execute(self):
        return self


def _create(body, owner_profile, fail_with=None):
    box = []

    class _SB:
        @staticmethod
        def table(name):
            assert name == 'manual_pins'
            return _FakeInsert(box, fail_with)

    with patch.object(A, 'SB_AVAILABLE', True), \
         patch.object(A, 'sb', _SB()), \
         patch.object(A, '_profile_load',
                      return_value={'profile': owner_profile}), \
         patch.object(A, '_hangout_notify_nearby') as notify, \
         A.app.test_request_context(f'/api/user/manual-pins/{OWNER}',
                                    json=body, method='POST'):
        resp = A.create_manual_pin(OWNER)
    payload = resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()
    status = resp[1] if isinstance(resp, tuple) else 200
    return payload, status, box, notify


def test_create_mit_filter_speichert_aufgeloeste_zielgruppe():
    body = {'iata': 'FRA', 'note': 'Kanu',
            'audience': {'airline': 'same', 'base': 'same',
                         'roles': ['cabin'],
                         'note': 'sportlich, Lust auf Kanu'}}
    payload, status, box, _ = _create(body, LH_FRA_CABIN)
    assert status == 200 and payload['ok'] is True
    stored = box[0]['audience']
    assert stored['airline'] == 'LUFTHANSA'
    assert stored['base'] == 'FRA'
    assert stored['roles'] == ['cabin']
    assert stored['note'] == 'sportlich, Lust auf Kanu'
    assert payload['pin']['audience_label'] == 'Nur Lufthansa · Base FRA · Kabine'


def test_create_ohne_filter_bleibt_offen():
    payload, status, box, _ = _create({'iata': 'FRA'}, LH_FRA_CABIN)
    assert status == 200
    assert 'audience' not in box[0]
    assert payload['pin']['audience'] is None


def test_create_eingeschraenkt_pusht_nicht():
    _, _, _, notify = _create(
        {'iata': 'FRA', 'audience': {'airline': 'same'}}, LH_FRA_CABIN)
    notify.assert_not_called()


def test_create_offen_pusht_weiterhin():
    _, _, _, notify = _create(
        {'iata': 'FRA', 'audience': {'note': 'kanu'}}, LH_FRA_CABIN)
    notify.assert_called_once()


def test_create_ohne_migration_speichert_keinen_offenen_hangout():
    """Fehlt die audience-Spalte, wird der eingeschränkte Hangout ABGELEHNT —
    nie still öffentlich gespeichert."""
    payload, status, _, _ = _create(
        {'iata': 'FRA', 'audience': {'airline': 'same'}}, LH_FRA_CABIN,
        fail_with="PGRST204 Could not find the 'audience' column")
    assert status == 503
    assert payload['error'] == 'audience_unsupported'
