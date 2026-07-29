"""KREISE — Selbst-Zuordnung statt Demografie (Owner 2026-07-29).

„Damit löst der Owner ‚nur Deutschsprachige' / ‚nur Frauen' elegant über
Selbst-Zuordnung statt Profil-Kategorisierung." Ein Kreis ist ein frei
benannter Topf, dem man FREIWILLIG beitritt; ein Hangout kann an ihn
adressiert werden (`audience.circle_id`).

Abgedeckt:
  * Erstellen (Ersteller ist automatisch aktives Mitglied), Beitreten,
    Verlassen — inkl. Aufräumen eines leer gewordenen Kreises
  * `join_policy='request'`: Beitritt landet als `pending` und ist WIRKUNGSLOS,
    bis der Ersteller bestätigt
  * Mitglieder-ANZAHL ja, Mitglieder-LISTE nein; keine rohen Tokens
  * Kreis-Sichtbarkeit von Hangouts (fail-closed ohne Mitgliedschaft)
  * fehlende Migration → ehrliches `storage: 'unavailable'` /
    `circles_unsupported`, nie stilles „gespeichert"

SICHERHEIT: kein echter SB-Call — in-memory-Attrappe.
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


OWNER = 'AT-OWNER'
JOINER = 'AT-JOINER'
STRANGER = 'AT-STRANGER'


# ── In-memory-Supabase (nur die hier benutzten Operationen) ─────────────────

class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store, table, fail_with=None):
        self.store, self.table_name = store, table
        self.fail_with = fail_with
        self.filters = []
        self._mode = 'select'
        self._payload = None
        self._limit = None

    # -- builder --
    def select(self, *a, **k):
        self._mode = 'select'
        return self

    def insert(self, row):
        self._mode, self._payload = 'insert', row
        return self

    def upsert(self, row, on_conflict=None):
        self._mode, self._payload = 'upsert', row
        self._conflict = [c.strip() for c in (on_conflict or '').split(',') if c]
        return self

    def update(self, patch_):
        self._mode, self._payload = 'update', patch_
        return self

    def delete(self):
        self._mode = 'delete'
        return self

    def eq(self, col, val):
        self.filters.append(('eq', col, val))
        return self

    def in_(self, col, vals):
        self.filters.append(('in', col, list(vals)))
        return self

    def order(self, col, desc=False):
        return self

    def limit(self, n):
        self._limit = n
        return self

    # -- exec --
    def _rows(self):
        rows = self.store.setdefault(self.table_name, [])
        out = []
        for r in rows:
            ok = True
            for kind, col, val in self.filters:
                if kind == 'eq' and r.get(col) != val:
                    ok = False
                if kind == 'in' and r.get(col) not in val:
                    ok = False
            if ok:
                out.append(r)
        return out

    def execute(self):
        if self.fail_with:
            raise Exception(self.fail_with)
        rows = self.store.setdefault(self.table_name, [])
        if self._mode == 'select':
            hit = self._rows()
            return _Result(hit[:self._limit] if self._limit else hit)
        if self._mode == 'insert':
            rows.append(dict(self._payload))
            return _Result([self._payload])
        if self._mode == 'upsert':
            keys = getattr(self, '_conflict', []) or []
            for r in rows:
                if keys and all(r.get(k) == self._payload.get(k) for k in keys):
                    r.update(self._payload)
                    return _Result([r])
            rows.append(dict(self._payload))
            return _Result([self._payload])
        if self._mode == 'update':
            hit = self._rows()
            for r in hit:
                r.update(self._payload)
            return _Result(hit)
        if self._mode == 'delete':
            hit = self._rows()
            self.store[self.table_name] = [r for r in rows if r not in hit]
            return _Result(hit)
        raise AssertionError(self._mode)


class _FakeSB:
    def __init__(self, store, fail_with=None):
        self.store, self.fail_with = store, fail_with

    def table(self, name):
        return _Query(self.store, name, self.fail_with)


def _sb(store=None, fail_with=None):
    store = store if store is not None else {}
    return store, patch.multiple(
        A, SB_AVAILABLE=True, sb=_FakeSB(store, fail_with))


def _call(fn, path, method='GET', json_body=None, **kwargs):
    with A.app.test_request_context(path, method=method, json=json_body):
        resp = fn(**kwargs)
    if isinstance(resp, tuple):
        return resp[0].get_json(), resp[1]
    return resp.get_json(), 200


def _create(store, token=OWNER, name='Deutschsprachig', policy='open'):
    return _call(A.create_crew_circle, f'/api/user/circles/{token}',
                 'POST', {'name': name, 'join_policy': policy, 'emoji': '🇩🇪'},
                 token=token)


# ── Erstellen ───────────────────────────────────────────────────────────────

def test_create_ersteller_ist_automatisch_mitglied():
    store, ctx = _sb()
    with ctx:
        payload, status = _create(store)
        assert status == 200 and payload['ok'] is True
        c = payload['circle']
        assert c['name'] == 'Deutschsprachig'
        assert c['emoji'] == '🇩🇪'
        assert c['member_count'] == 1
        assert c['joined'] is True and c['mine'] is True
        assert A._circles_of_user(OWNER) == {c['id']}


def test_create_braucht_einen_namen():
    store, ctx = _sb()
    with ctx:
        payload, status = _create(store, name=' ')
    assert status == 400 and payload['error'] == 'name_required'


def test_create_deckelt_die_anzahl_eigener_kreise():
    store, ctx = _sb()
    with ctx:
        for i in range(A._CREW_CIRCLE_MAX_OWNED):
            _, status = _create(store, name=f'Kreis {i}')
            assert status == 200
        payload, status = _create(store, name='einer zu viel')
    assert status == 409 and payload['error'] == 'too_many_circles'


def test_create_verwirft_kaputte_farbe():
    store, ctx = _sb()
    with ctx:
        payload, _ = _call(A.create_crew_circle, f'/api/user/circles/{OWNER}',
                           'POST', {'name': 'Kanu', 'color': 'javascript:x'},
                           token=OWNER)
    assert payload['circle']['color'] is None


# ── Beitreten / Verlassen ───────────────────────────────────────────────────

def test_join_offener_kreis_ist_sofort_aktiv():
    store, ctx = _sb()
    with ctx:
        cid = _create(store)[0]['circle']['id']
        payload, status = _call(
            A.join_crew_circle, f'/api/user/circles/{JOINER}/{cid}/join',
            'POST', {}, token=JOINER, circle_id=cid)
        assert status == 200
        assert payload['joined'] is True and payload['pending'] is False
        assert A._circles_of_user(JOINER) == {cid}


def test_join_ist_idempotent():
    store, ctx = _sb()
    with ctx:
        cid = _create(store)[0]['circle']['id']
        for _ in range(3):
            _call(A.join_crew_circle, f'/api/user/circles/{JOINER}/{cid}/join',
                  'POST', {}, token=JOINER, circle_id=cid)
        detail, _ = _call(A.get_crew_circle, f'/api/user/circles/{OWNER}/{cid}',
                          token=OWNER, circle_id=cid)
    assert detail['circle']['member_count'] == 2


def test_join_unbekannter_kreis_ist_404():
    store, ctx = _sb()
    with ctx:
        payload, status = _call(
            A.join_crew_circle, f'/api/user/circles/{JOINER}/nope/join',
            'POST', {}, token=JOINER, circle_id='nope')
    assert status == 404 and payload['error'] == 'not_found'


def test_leave_entfernt_die_mitgliedschaft():
    store, ctx = _sb()
    with ctx:
        cid = _create(store)[0]['circle']['id']
        _call(A.join_crew_circle, f'/api/user/circles/{JOINER}/{cid}/join',
              'POST', {}, token=JOINER, circle_id=cid)
        payload, status = _call(
            A.leave_crew_circle, f'/api/user/circles/{JOINER}/{cid}/leave',
            'POST', {}, token=JOINER, circle_id=cid)
        assert status == 200 and payload['joined'] is False
        assert payload['circle_deleted'] is False   # der Ersteller ist noch drin
        assert A._circles_of_user(JOINER) == set()


def test_leave_des_letzten_mitglieds_raeumt_den_kreis_ab():
    """Ein leerer Kreis ist nur noch ein Name, der Hangouts unsichtbar macht."""
    store, ctx = _sb()
    with ctx:
        cid = _create(store)[0]['circle']['id']
        payload, _ = _call(
            A.leave_crew_circle, f'/api/user/circles/{OWNER}/{cid}/leave',
            'POST', {}, token=OWNER, circle_id=cid)
        assert payload['circle_deleted'] is True
        assert A._circle_load(cid) is None


# ── „Auf Anfrage" ───────────────────────────────────────────────────────────

def test_request_kreis_beitritt_ist_erst_pending():
    store, ctx = _sb()
    with ctx:
        cid = _create(store, policy='request')[0]['circle']['id']
        payload, _ = _call(
            A.join_crew_circle, f'/api/user/circles/{JOINER}/{cid}/join',
            'POST', {}, token=JOINER, circle_id=cid)
        assert payload['pending'] is True and payload['joined'] is False
        # WIRKUNGSLOS bis bestätigt: er ist in keinem Kreis.
        assert A._circles_of_user(JOINER) == set()


def test_request_bestaetigen_macht_aktiv():
    store, ctx = _sb()
    with ctx:
        cid = _create(store, policy='request')[0]['circle']['id']
        _call(A.join_crew_circle, f'/api/user/circles/{JOINER}/{cid}/join',
              'POST', {}, token=JOINER, circle_id=cid)
        with patch.object(A, '_profiles_load_bulk',
                          return_value={JOINER: {'name': 'Jo'}}):
            detail, _ = _call(A.get_crew_circle,
                              f'/api/user/circles/{OWNER}/{cid}',
                              token=OWNER, circle_id=cid)
        reqs = detail['circle']['requests']
        assert [r['name'] for r in reqs] == ['Jo']
        mid = reqs[0]['match_id']
        assert JOINER not in str(detail)          # kein rohes Token
        payload, status = _call(
            A.decide_crew_circle_request,
            f'/api/user/circles/{OWNER}/{cid}/requests/{mid}',
            'POST', {'approve': True}, token=OWNER, circle_id=cid,
            match_id=mid)
        assert status == 200 and payload['approved'] is True
        assert A._circles_of_user(JOINER) == {cid}


def test_request_ablehnen_entfernt_die_anfrage():
    store, ctx = _sb()
    with ctx:
        cid = _create(store, policy='request')[0]['circle']['id']
        _call(A.join_crew_circle, f'/api/user/circles/{JOINER}/{cid}/join',
              'POST', {}, token=JOINER, circle_id=cid)
        mid = A._circle_match_id(JOINER)
        payload, status = _call(
            A.decide_crew_circle_request,
            f'/api/user/circles/{OWNER}/{cid}/requests/{mid}',
            'POST', {'approve': False}, token=OWNER, circle_id=cid,
            match_id=mid)
        assert status == 200 and payload['approved'] is False
        assert A._circles_of_user(JOINER) == set()
        assert A._circle_membership_rows(circle_ids=[cid],
                                         user_token=JOINER) == []


def test_nur_der_ersteller_darf_entscheiden():
    store, ctx = _sb()
    with ctx:
        cid = _create(store, policy='request')[0]['circle']['id']
        _call(A.join_crew_circle, f'/api/user/circles/{JOINER}/{cid}/join',
              'POST', {}, token=JOINER, circle_id=cid)
        mid = A._circle_match_id(JOINER)
        payload, status = _call(
            A.decide_crew_circle_request,
            f'/api/user/circles/{STRANGER}/{cid}/requests/{mid}',
            'POST', {'approve': True}, token=STRANGER, circle_id=cid,
            match_id=mid)
    assert status == 403 and payload['error'] == 'not_owner'


def test_nicht_ersteller_sieht_keine_anfragen():
    store, ctx = _sb()
    with ctx:
        cid = _create(store, policy='request')[0]['circle']['id']
        _call(A.join_crew_circle, f'/api/user/circles/{JOINER}/{cid}/join',
              'POST', {}, token=JOINER, circle_id=cid)
        detail, _ = _call(A.get_crew_circle,
                          f'/api/user/circles/{STRANGER}/{cid}',
                          token=STRANGER, circle_id=cid)
    assert 'requests' not in detail['circle']


# ── Liste ───────────────────────────────────────────────────────────────────

def test_liste_zeigt_anzahl_aber_keine_mitglieder():
    store, ctx = _sb()
    with ctx:
        cid = _create(store)[0]['circle']['id']
        _call(A.join_crew_circle, f'/api/user/circles/{JOINER}/{cid}/join',
              'POST', {}, token=JOINER, circle_id=cid)
        data, status = _call(A.list_crew_circles,
                             f'/api/user/circles/{STRANGER}', token=STRANGER)
    assert status == 200 and data['storage'] == 'ok'
    c = data['circles'][0]
    assert c['member_count'] == 2
    assert c['joined'] is False and c['mine'] is False
    assert 'members' not in c
    assert JOINER not in str(data) and OWNER not in str(data)


def test_liste_kennt_meinen_pending_status():
    store, ctx = _sb()
    with ctx:
        cid = _create(store, policy='request')[0]['circle']['id']
        _call(A.join_crew_circle, f'/api/user/circles/{JOINER}/{cid}/join',
              'POST', {}, token=JOINER, circle_id=cid)
        data, _ = _call(A.list_crew_circles, f'/api/user/circles/{JOINER}',
                        token=JOINER)
    c = data['circles'][0]
    assert c['pending'] is True and c['joined'] is False
    assert c['member_count'] == 1   # pending zählt NICHT mit


# ── Fehlende Migration: ehrlich degradieren ─────────────────────────────────

_MISSING = "PGRST205 Could not find the table 'public.crew_circles'"


def test_liste_ohne_migration_sagt_unavailable():
    store, ctx = _sb(fail_with=_MISSING)
    with ctx:
        data, status = _call(A.list_crew_circles, f'/api/user/circles/{OWNER}',
                             token=OWNER)
    assert status == 200
    assert data['storage'] == 'unavailable' and data['circles'] == []


def test_create_ohne_migration_behauptet_nicht_gespeichert():
    store, ctx = _sb(fail_with=_MISSING)
    with ctx:
        payload, status = _create(store)
    assert status == 503 and payload['error'] == 'circles_unsupported'


def test_circles_of_user_ohne_migration_ist_leer_fail_closed():
    store, ctx = _sb(fail_with=_MISSING)
    with ctx:
        assert A._circles_of_user(OWNER) == set()


# ── Kreis-Sichtbarkeit eines Hangouts (Ende-zu-Ende) ────────────────────────

def _pin(pid, owner, circle_id):
    return {'id': pid, 'user_token': owner, 'iata_code': 'FRA',
            'lat': 50.0, 'lng': 8.5, 'pin_date': None, 'note': 'Bier?',
            'audience': {'v': 1, 'circle_id': circle_id,
                         'circle_name': 'Deutschsprachig'}}


def _hangouts_for(token, pins):
    with patch.object(A, '_hangouts_load_all_active', return_value=pins), \
         patch.object(A, '_friends_load', return_value={'friends': []}), \
         patch.object(A, '_profiles_load_bulk', return_value={}), \
         patch.object(A, '_profile_load', return_value={'profile': {}}), \
         A.app.test_request_context(f'/api/user/hangouts/{token}'):
        return A.list_hangouts(token).get_json()


def test_kreis_hangout_nur_fuer_mitglieder_sichtbar():
    store, ctx = _sb()
    with ctx:
        cid = _create(store)[0]['circle']['id']
        _call(A.join_crew_circle, f'/api/user/circles/{JOINER}/{cid}/join',
              'POST', {}, token=JOINER, circle_id=cid)
        pins = [_pin('k', OWNER, cid)]
        assert [h['id'] for h in _hangouts_for(JOINER, pins)['hangouts']] == ['k']
        assert _hangouts_for(STRANGER, pins)['hangouts'] == []
        # Der Ersteller sieht seinen eigenen immer.
        assert [h['id'] for h in _hangouts_for(OWNER, pins)['hangouts']] == ['k']


def test_wer_den_kreis_verlaesst_sieht_den_hangout_nicht_mehr():
    store, ctx = _sb()
    with ctx:
        cid = _create(store)[0]['circle']['id']
        _call(A.join_crew_circle, f'/api/user/circles/{JOINER}/{cid}/join',
              'POST', {}, token=JOINER, circle_id=cid)
        pins = [_pin('k', OWNER, cid)]
        assert _hangouts_for(JOINER, pins)['hangouts'] != []
        _call(A.leave_crew_circle, f'/api/user/circles/{JOINER}/{cid}/leave',
              'POST', {}, token=JOINER, circle_id=cid)
        assert _hangouts_for(JOINER, pins)['hangouts'] == []


def test_kreis_hangout_label_und_kein_geo_push():
    aud = {'v': 1, 'circle_id': 'c1', 'circle_name': 'Deutschsprachig'}
    assert A._hangout_audience_is_restricted(aud) is True
    assert A._hangout_audience_label(aud) == 'Kreis Deutschsprachig'
