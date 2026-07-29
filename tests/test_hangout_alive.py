"""Hangout „lebendig machen" (Owner 2026-07-29).

„Denk drüber nach, wie wir Hangouts BESSER machen können — auch der Gruppenchat,
der sich öffnet, hat keine Informationen, sieht langweilig und uneinladend aus."
Demografisches Targeting ist ausdrücklich VOM TISCH; das Profil bleibt
unverändert. Diese Runde bringt daher drei Dinge, die nichts Neues über
MENSCHEN erheben:

  * VIBE-TAGS — der Ersteller sagt, was der PLAN ist („Sportlich", „Nightlife").
    Filtert NICHT, lebt im vorhandenen offenen `audience`-jsonb (keine Migration).
  * ZEITFENSTER (`meta`) — bisher war nur das Ablauf-DATUM gespeichert, die
    eingegebene Uhrzeit ging verloren.
  * ZUSAGEN (`attendees`) — vorher gab es GAR KEINEN Beitritts-Mechanismus.

Plus der Detail-Endpoint, aus dem der Gruppenchat seinen Kontext-Kopf zieht.

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


VIEWER = 'viewer-token'
OWNER = 'owner-token'
OTHER = 'other-token'
LH_FRA_CABIN = {'airline': 'Lufthansa', 'homebase': 'FRA', 'position': 'PU'}
LX_ZRH_CABIN = {'airline': 'SWISS', 'homebase': 'ZRH', 'position': 'FB'}


def _pin(pid='h1', owner=OWNER, audience=None, attendees=None, meta=None,
         iata='FRA'):
    row = {'id': pid, 'user_token': owner, 'iata_code': iata,
           'lat': 50.0, 'lng': 8.5, 'pin_date': None, 'note': '🏃 Kanu',
           'created_at': A.datetime.now(A.timezone.utc).isoformat()}
    if audience is not None:
        row['audience'] = audience
    if attendees is not None:
        row['attendees'] = attendees
    if meta is not None:
        row['meta'] = meta
    return row


# ── Vibes: Normalisierung ───────────────────────────────────────────────────

def test_vibes_whitelist_und_kanonische_reihenfolge():
    assert A._hangout_vibes_normalize(
        ['nightlife', 'sportlich']) == ['sportlich', 'nightlife']


def test_vibes_ignorieren_freitext_und_muell():
    assert A._hangout_vibes_normalize(['sportlich', 'sauf-tour', 42]) == ['sportlich']
    assert A._hangout_vibes_normalize('sportlich') == []
    assert A._hangout_vibes_normalize(None) == []


def test_vibes_deduplizieren_und_deckel_bei_drei():
    got = A._hangout_vibes_normalize(
        ['bar', 'bar', 'kaffee', 'essen', 'nightlife', 'sportlich'])
    assert len(got) == A._HANGOUT_MAX_VIBES == 3
    assert got == ['sportlich', 'bar', 'kaffee']


def test_vibes_labels_sind_deutsch():
    assert A._hangout_vibe_labels(['sportlich', 'nightlife']) == [
        'Sportlich', 'Nightlife']
    assert A._hangout_vibe_labels(['gibts-nicht']) == []


def test_vibes_landen_in_der_audience():
    aud = A._hangout_audience_normalize({'vibes': ['sportlich']}, LH_FRA_CABIN)
    assert aud == {'v': 1, 'vibes': ['sportlich']}


def test_vibes_alleine_schraenken_NICHT_ein():
    """Ein Vibe beschreibt den Plan — er darf niemanden aussperren."""
    aud = A._hangout_audience_normalize({'vibes': ['nightlife']}, LH_FRA_CABIN)
    assert A._hangout_audience_is_restricted(aud) is False
    assert A._hangout_audience_label(aud) is None
    # …und der Hangout bleibt für JEDEN sichtbar.
    assert A._hangout_audience_matches(aud, {}) is True
    assert A._hangout_audience_matches(aud, LX_ZRH_CABIN) is True


def test_vibes_neben_echtem_filter():
    aud = A._hangout_audience_normalize(
        {'airline': 'same', 'vibes': ['essen']}, LH_FRA_CABIN)
    assert aud['airline'] == 'LUFTHANSA' and aud['vibes'] == ['essen']
    assert A._hangout_audience_is_restricted(aud) is True


# ── Zeitfenster ─────────────────────────────────────────────────────────────

def test_meta_normalize_nimmt_nur_bekannte_keys():
    got = A._hangout_meta_normalize(
        {'starts_at': '2026-08-01T14:00:00Z', 'ends_at': '2026-08-01T18:00:00Z',
         'geheim': 'x'})
    assert got == {'v': 1, 'starts_at': '2026-08-01T14:00:00Z',
                   'ends_at': '2026-08-01T18:00:00Z'}


def test_meta_normalize_leer_ist_none():
    assert A._hangout_meta_normalize({}) is None
    assert A._hangout_meta_normalize({'starts_at': '  '}) is None
    assert A._hangout_meta_normalize('2026-08-01') is None


def test_meta_of_row_tolerant():
    assert A._hangout_meta_of_row({'meta': {'v': 1}}) == {'v': 1}
    assert A._hangout_meta_of_row({'meta': '{"v":1}'}) == {'v': 1}
    assert A._hangout_meta_of_row({'meta': 'kaputt'}) is None
    assert A._hangout_meta_of_row({}) is None          # Spalte fehlt


# ── Zusagen ─────────────────────────────────────────────────────────────────

def test_attendees_of_row_tolerant_und_dedupliziert():
    assert A._hangout_attendees_of_row({'attendees': ['a', 'a', 'b']}) == ['a', 'b']
    assert A._hangout_attendees_of_row({'attendees': '["a"]'}) == ['a']
    assert A._hangout_attendees_of_row({'attendees': 'kaputt'}) == []
    assert A._hangout_attendees_of_row({}) == []       # Spalte fehlt


def test_people_geben_NIE_ein_rohes_token_zurueck():
    """Das Token IST das Bearer-Credential — dieselbe Regel wie owner_token."""
    profs = {OTHER: {'name': 'Tibor', 'avatar_url': 'https://x/y.jpg'}}
    people = A._hangout_people([VIEWER, OTHER], VIEWER, profiles=profs)
    blob = repr(people)
    assert VIEWER not in blob and OTHER not in blob
    assert people[0]['name'] == 'Du' and people[0]['mine'] is True
    assert people[1]['name'] == 'Tibor'
    assert people[1]['avatar_url'] == 'https://x/y.jpg'
    assert len(people[1]['match_id']) == 16


def test_people_ohne_profil_heissen_crew_nicht_token():
    people = A._hangout_people([OTHER], VIEWER, profiles={})
    assert people[0]['name'] == 'Crew'


# ── Anzeige-Felder in den Listen-Endpoints ──────────────────────────────────

def _list_hangouts(pins, viewer_profile, token=VIEWER):
    with patch.object(A, '_hangouts_load_all_active', return_value=pins), \
         patch.object(A, '_friends_load', return_value={'friends': []}), \
         patch.object(A, '_profiles_load_bulk', return_value={}), \
         patch.object(A, '_profile_load',
                      return_value={'profile': viewer_profile}), \
         A.app.test_request_context(f'/api/user/hangouts/{token}'):
        return A.list_hangouts(token).get_json()


def test_liste_liefert_vibes_zeitfenster_und_zusagen_zaehler():
    row = _pin(audience={'v': 1, 'vibes': ['sportlich', 'essen']},
               attendees=[OWNER, VIEWER],
               meta={'v': 1, 'starts_at': '2026-08-01T14:00:00Z'})
    h = _list_hangouts([row], LH_FRA_CABIN)['hangouts'][0]
    assert h['vibes'] == ['sportlich', 'essen']
    assert h['vibe_labels'] == ['Sportlich', 'Essen gehen']
    assert h['meta']['starts_at'] == '2026-08-01T14:00:00Z'
    assert h['attendee_count'] == 2
    assert h['attending'] is True


def test_liste_ohne_migration_bleibt_ruhig():
    """Alt-Zeile ohne meta/attendees: keine Fehler, nur ehrliche Werte.

    GEÄNDERT 2026-07-29: der ERSTELLER zählt immer mit (er steht als
    `user_token` auf der Zeile — dafür braucht es die attendees-Spalte nicht).
    „0 dabei" auf einem Treffpunkt, zu dem jemand eingeladen hat, wäre falsch.
    Der VIEWER ist hier nicht der Ersteller → `attending` bleibt False.
    """
    h = _list_hangouts([_pin()], LH_FRA_CABIN)['hangouts'][0]
    assert h['vibes'] == [] and h['meta'] is None
    assert h['attendee_count'] == 1
    assert h['attending'] is False and h['is_owner'] is False


def test_liste_ersteller_ist_immer_dabei():
    """Der Ersteller sieht seinen eigenen Treff als „dabei" — ohne je zu tippen."""
    h = _list_hangouts([_pin()], LH_FRA_CABIN, token=OWNER)['hangouts'][0]
    assert h['attendee_count'] == 1
    assert h['attending'] is True and h['is_owner'] is True


def test_liste_vibes_oeffnen_keine_hintertuer():
    """Vibes dürfen einen eingeschränkten Hangout nicht sichtbar machen —
    und einen offenen nicht verstecken."""
    restricted = _pin('lh', audience={'v': 1, 'airline': 'LUFTHANSA',
                                      'vibes': ['bar']})
    offen = _pin('offen', audience={'v': 1, 'vibes': ['bar']})
    ids = [h['id'] for h in _list_hangouts([restricted, offen],
                                           LX_ZRH_CABIN)['hangouts']]
    assert ids == ['offen']


# ── Detail-Endpoint (Quelle für den Chat-Kontext-Kopf) ──────────────────────

def _detail(row, viewer_profile, token=VIEWER, profiles=None):
    with patch.object(A, '_hangout_load_one', return_value=row), \
         patch.object(A, '_friends_load', return_value={'friends': []}), \
         patch.object(A, '_profiles_load_bulk', return_value=profiles or {}), \
         patch.object(A, '_profile_load',
                      return_value={'profile': viewer_profile}), \
         A.app.test_request_context(f'/api/user/hangouts/{token}/h1'):
        resp = A.get_hangout_detail(token, 'h1')
    payload = resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()
    status = resp[1] if isinstance(resp, tuple) else 200
    return payload, status


def test_detail_traegt_den_ganzen_kontext():
    row = _pin(audience={'v': 1, 'vibes': ['sportlich'],
                         'note': 'sportlich, Lust auf Kanu'},
               attendees=[OWNER, VIEWER],
               meta={'v': 1, 'starts_at': '2026-08-01T14:00:00Z',
                     'ends_at': '2026-08-01T18:00:00Z'})
    payload, status = _detail(row, LH_FRA_CABIN,
                              profiles={OWNER: {'name': 'Lars'}})
    assert status == 200
    h = payload['hangout']
    assert h['iata'] == 'FRA' and h['note'] == '🏃 Kanu'
    assert h['owner_name'] == 'Lars'
    assert h['vibe_labels'] == ['Sportlich']
    assert h['audience']['note'] == 'sportlich, Lust auf Kanu'
    assert h['meta']['ends_at'] == '2026-08-01T18:00:00Z'
    assert h['attendee_count'] == 2 and h['attending'] is True
    assert [p['name'] for p in h['attendees']] == ['Lars', 'Du']
    assert h['lat'] == 50.0 and h['lng'] == 8.5      # Karten-Vignette


def test_detail_verheimlicht_fremde_tokens():
    row = _pin(attendees=[OWNER, OTHER])
    payload, _ = _detail(row, LH_FRA_CABIN)
    blob = repr(payload)
    assert OWNER not in blob and OTHER not in blob
    assert payload['hangout']['owner_token'] is None


def test_detail_zielgruppe_verfehlt_ist_404_nicht_403():
    """403 würde die Existenz des Hangouts verraten."""
    row = _pin(audience={'v': 1, 'airline': 'LUFTHANSA'})
    payload, status = _detail(row, LX_ZRH_CABIN)
    assert status == 404 and payload['error'] == 'not_found'


def test_detail_ersteller_sieht_seinen_hangout_immer():
    row = _pin(owner=VIEWER, audience={'v': 1, 'airline': 'LUFTHANSA'})
    payload, status = _detail(row, {})
    assert status == 200 and payload['hangout']['mine'] is True
    assert payload['hangout']['owner_name'] == 'Du'


def test_detail_abgelaufener_hangout_ist_weg():
    row = _pin()
    row['pin_date'] = '2020-01-01'
    _, status = _detail(row, LH_FRA_CABIN)
    assert status == 404


def test_detail_unbekannte_id_ist_404():
    _, status = _detail(None, LH_FRA_CABIN)
    assert status == 404


# ── „Bin dabei" ─────────────────────────────────────────────────────────────

class _FakeUpdate:
    def __init__(self, box, fail_with=None):
        self.box = box
        self.fail_with = fail_with

    def update(self, patch_):
        self.box.append(patch_)
        if self.fail_with:
            raise Exception(self.fail_with)
        return self

    def eq(self, *_a, **_k):
        return self

    def execute(self):
        return self


def _join(row, body, viewer_profile=LH_FRA_CABIN, token=VIEWER, fail_with=None):
    box = []

    class _SB:
        @staticmethod
        def table(name):
            assert name == 'manual_pins'
            return _FakeUpdate(box, fail_with)

    with patch.object(A, 'SB_AVAILABLE', True), \
         patch.object(A, 'sb', _SB()), \
         patch.object(A, '_hangout_load_one', return_value=row), \
         patch.object(A, '_friends_load', return_value={'friends': []}), \
         patch.object(A, '_profiles_load_bulk', return_value={}), \
         patch.object(A, '_profile_load',
                      return_value={'profile': viewer_profile}), \
         A.app.test_request_context(f'/api/user/hangouts/{token}/h1/join',
                                    json=body, method='POST'):
        resp = A.join_hangout(token, 'h1')
    payload = resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()
    status = resp[1] if isinstance(resp, tuple) else 200
    return payload, status, box


def test_join_traegt_den_viewer_ein():
    payload, status, box = _join(_pin(attendees=[OWNER]), {'join': True})
    assert status == 200 and payload['attending'] is True
    assert payload['attendee_count'] == 2
    assert box[0]['attendees'] == [OWNER, VIEWER]


def test_join_default_ist_zusagen():
    payload, _, box = _join(_pin(attendees=[OWNER]), {})
    assert payload['attending'] is True and box[0]['attendees'][-1] == VIEWER


def test_join_ist_idempotent_und_schreibt_nicht_doppelt():
    payload, status, box = _join(_pin(attendees=[OWNER, VIEWER]), {'join': True})
    assert status == 200 and payload['attendee_count'] == 2
    assert box == []           # kein sinnloser Write


def test_absagen_entfernt_nur_mich():
    payload, _, box = _join(_pin(attendees=[OWNER, VIEWER]), {'join': False})
    assert payload['attending'] is False
    assert box[0]['attendees'] == [OWNER]


def test_join_antwortet_ohne_rohe_tokens():
    payload, _, _ = _join(_pin(attendees=[OWNER]), {'join': True})
    assert OWNER not in repr(payload) and VIEWER not in repr(payload)


def test_join_auf_fremde_zielgruppe_ist_404():
    row = _pin(audience={'v': 1, 'airline': 'LUFTHANSA'}, attendees=[OWNER])
    payload, status, box = _join(row, {'join': True}, viewer_profile=LX_ZRH_CABIN)
    assert status == 404 and box == []


def test_join_ohne_migration_luegt_nicht():
    """Fehlt die attendees-Spalte, sagen wir das — statt „gespeichert"."""
    payload, status, _ = _join(
        _pin(attendees=[OWNER]), {'join': True},
        fail_with="PGRST204 Could not find the 'attendees' column")
    assert status == 503 and payload['error'] == 'attendees_unsupported'


def test_join_echter_schreibfehler_ist_kein_migrations_hinweis():
    payload, status, _ = _join(_pin(attendees=[OWNER]), {'join': True},
                               fail_with='connection reset by peer')
    assert status == 500 and payload['error'] == 'join_failed'


# ── Der Ersteller ist immer dabei (Owner 2026-07-29) ────────────────────────

def test_ersteller_steht_auch_ohne_gespeicherte_zusage_in_der_liste():
    """Alt-Zeile ohne attendees-Spalte: der Ersteller zählt trotzdem."""
    assert A._hangout_attendees_of_row({'user_token': OWNER}) == [OWNER]


def test_ersteller_wird_nicht_doppelt_gezaehlt():
    assert A._hangout_attendees_of_row(
        {'user_token': OWNER, 'attendees': [VIEWER, OWNER]}) == [OWNER, VIEWER]


def test_ersteller_kann_sich_nicht_austragen():
    """Wer einlädt, ist der Treffpunkt — Austragen wäre eine stille Lüge."""
    payload, status, box = _join(_pin(attendees=[OWNER]), {'join': False},
                                 token=OWNER)
    assert status == 409 and payload['error'] == 'owner_cannot_leave'
    assert payload['attending'] is True
    assert box == []           # nichts geschrieben


def test_ersteller_zusagen_ist_idempotent_ohne_write():
    payload, status, box = _join(_pin(), {'join': True}, token=OWNER)
    assert status == 200 and payload['attending'] is True
    assert payload['attendee_count'] == 1 and box == []


# ── Hangout entfernen: hart löschen vs. weich absagen ───────────────────────

class _FakeMutate:
    """Sammelt update()/delete()-Aufrufe getrennt ein."""

    def __init__(self, updates, deletes, fail_update=None):
        self.updates = updates
        self.deletes = deletes
        self.fail_update = fail_update

    def update(self, patch_):
        self.updates.append(patch_)
        if self.fail_update:
            raise Exception(self.fail_update)
        return self

    def delete(self):
        self.deletes.append(True)
        return self

    def eq(self, *_a, **_k):
        return self

    def execute(self):
        return self


def _delete(row, token=OWNER, fail_update=None):
    updates, deletes = [], []

    class _SB:
        @staticmethod
        def table(name):
            assert name == 'manual_pins'
            return _FakeMutate(updates, deletes, fail_update)

    with patch.object(A, 'SB_AVAILABLE', True), \
         patch.object(A, 'sb', _SB()), \
         patch.object(A, '_hangout_load_one', return_value=row), \
         A.app.test_request_context(
             f'/api/user/manual-pins/{token}/h1/delete', method='POST'):
        resp = A.delete_manual_pin(token, 'h1')
    payload = resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()
    status = resp[1] if isinstance(resp, tuple) else 200
    return payload, status, updates, deletes


def test_delete_ohne_zusagen_loescht_hart():
    payload, status, updates, deletes = _delete(_pin(attendees=[OWNER]))
    assert status == 200 and payload['cancelled'] is False
    assert deletes == [True] and updates == []


def test_delete_mit_fremden_zusagen_sagt_weich_ab():
    """Wer zugesagt hat, soll nicht ohne Spur dastehen."""
    payload, status, updates, deletes = _delete(
        _pin(attendees=[OWNER, VIEWER]))
    assert status == 200 and payload['cancelled'] is True
    assert deletes == []
    assert updates[0]['meta']['cancelled_at']


def test_delete_fremder_hangout_ist_404_und_ruehrt_nichts_an():
    payload, status, updates, deletes = _delete(_pin(owner=OTHER), token=OWNER)
    assert status == 404 and payload['error'] == 'not_found'
    assert updates == [] and deletes == []


def test_delete_unbekannte_id_ist_idempotent():
    payload, status, _, deletes = _delete(None)
    assert status == 200 and payload['ok'] is True and deletes == []


def test_delete_ohne_meta_spalte_faellt_auf_hartes_loeschen_zurueck():
    """Absage nicht speicherbar → NICHT „abgesagt" behaupten."""
    payload, status, updates, deletes = _delete(
        _pin(attendees=[OWNER, VIEWER]),
        fail_update="PGRST204 Could not find the 'meta' column")
    assert status == 200 and payload['cancelled'] is False
    assert updates and deletes == [True]


def test_abgesagter_hangout_faellt_aus_den_listen():
    row = _pin(meta={'v': 1, 'cancelled_at': '2026-07-29T10:00:00+00:00'})
    assert A._hangout_cancelled_at(row)
    assert A._hangout_is_listable(row) is False
    assert _list_hangouts([row], LH_FRA_CABIN)['hangouts'] == []


def test_abgesagter_hangout_bleibt_im_detail_erreichbar():
    """Damit der Gruppenchat der Zugesagten „Abgesagt" zeigt statt eines 404."""
    row = _pin(attendees=[OWNER, VIEWER],
               meta={'v': 1, 'cancelled_at': '2026-07-29T10:00:00+00:00'})
    payload, status = _detail(row, LH_FRA_CABIN)
    assert status == 200 and payload['hangout']['cancelled'] is True


# ── Erstellen ───────────────────────────────────────────────────────────────

class _FakeInsert:
    def __init__(self, box, fail_first=None):
        self.box = box
        self.fail_first = fail_first

    def insert(self, row):
        self.box.append(row)
        if self.fail_first and len(self.box) == 1:
            raise Exception(self.fail_first)
        return self

    def execute(self):
        return self


def _create(body, owner_profile=LH_FRA_CABIN, fail_first=None):
    box = []

    class _SB:
        @staticmethod
        def table(name):
            assert name == 'manual_pins'
            return _FakeInsert(box, fail_first)

    with patch.object(A, 'SB_AVAILABLE', True), \
         patch.object(A, 'sb', _SB()), \
         patch.object(A, '_profile_load',
                      return_value={'profile': owner_profile}), \
         patch.object(A, '_hangout_notify_nearby'), \
         A.app.test_request_context(f'/api/user/manual-pins/{OWNER}',
                                    json=body, method='POST'):
        resp = A.create_manual_pin(OWNER)
    payload = resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()
    status = resp[1] if isinstance(resp, tuple) else 200
    return payload, status, box


def test_create_speichert_vibes_zeitfenster_und_ersteller_als_zusage():
    payload, status, box = _create({
        'iata': 'FRA', 'note': '🏃 Kanu auf dem Spreewald',
        'audience': {'vibes': ['sportlich'], 'note': 'Lust auf Kanu'},
        'meta': {'starts_at': '2026-08-01T14:00:00Z',
                 'ends_at': '2026-08-01T18:00:00Z'}})
    assert status == 200
    row = box[0]
    assert row['audience']['vibes'] == ['sportlich']
    assert row['meta']['starts_at'] == '2026-08-01T14:00:00Z'
    assert row['attendees'] == [OWNER]      # „0 dabei" auf dem eigenen Treff wäre falsch
    assert payload['pin']['vibe_labels'] == ['Sportlich']
    assert payload['pin']['attendee_count'] == 1
    assert payload['pin']['attending'] is True
    assert payload['pin']['degraded'] is None


def test_create_ohne_meta_bleibt_schlank():
    _, status, box = _create({'iata': 'FRA'})
    assert status == 200
    assert 'meta' not in box[0] and 'audience' not in box[0]


def test_create_ohne_migration_legt_den_hangout_trotzdem_an():
    """Anzeige-Beiwerk darf das Anlegen NICHT verhindern — es fällt weg und
    das sagt die Antwort auch."""
    payload, status, box = _create(
        {'iata': 'FRA', 'audience': {'vibes': ['bar']},
         'meta': {'starts_at': '2026-08-01T20:00:00Z'}},
        fail_first="PGRST204 Could not find the 'meta' column")
    assert status == 200
    assert sorted(payload['pin']['degraded']) == ['attendees', 'audience', 'meta']
    # Die Zusage des ERSTELLERS überlebt die fehlende Migration: sie leitet sich
    # aus `user_token` ab, nicht aus der attendees-Spalte.
    assert payload['pin']['attendee_count'] == 1
    assert payload['pin']['attending'] is True
    assert 'meta' not in box[1] and 'attendees' not in box[1]
    assert box[1]['iata_code'] == 'FRA'     # der Hangout selbst ist da


def test_create_eingeschraenkt_ohne_migration_bleibt_abgelehnt():
    """Regression: eine ECHTE Einschränkung wird NIE stillschweigend
    fallengelassen — sonst stünde ein „nur meine Airline"-Treff für alle offen."""
    payload, status, box = _create(
        {'iata': 'FRA', 'audience': {'airline': 'same', 'vibes': ['bar']}},
        fail_first="PGRST204 Could not find the 'audience' column")
    assert status == 503 and payload['error'] == 'audience_unsupported'
    assert len(box) == 1                    # kein zweiter, offener Insert


def test_create_echter_insert_fehler_bleibt_500():
    payload, status, box = _create({'iata': 'FRA'},
                                   fail_first='connection reset by peer')
    assert status == 500 and payload['error'] == 'insert_failed'
    assert len(box) == 1


def test_create_vibes_alleine_pushen_weiterhin():
    """Vibes schränken nicht ein → der Geo-Push bleibt erlaubt."""
    with patch.object(A, 'SB_AVAILABLE', True), \
         patch.object(A, 'sb', type('S', (), {
             'table': staticmethod(lambda n: _FakeInsert([]))})()), \
         patch.object(A, '_profile_load', return_value={'profile': LH_FRA_CABIN}), \
         patch.object(A, '_hangout_notify_nearby') as notify, \
         A.app.test_request_context(
             f'/api/user/manual-pins/{OWNER}',
             json={'iata': 'FRA', 'audience': {'vibes': ['nightlife']}},
             method='POST'):
        A.create_manual_pin(OWNER)
    notify.assert_called_once()
