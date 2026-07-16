"""Hangout-Geo-Push (Owner-Wunsch 2026-07-16).

Wenn ein User einen öffentlichen Treffpunkt-Pin (Hangout) erstellt, sollen alle
AeroX-User im ~100-km-Umkreis EINEN Push bekommen. Diese Tests decken die reine
Geo-Selektion (_users_near), den Empfänger-Deckel, den Ersteller-Cooldown und
das Flag-Gate (HANGOUT_GEO_PUSH) ab.

SICHERHEIT: KEIN echter APNs-/SB-Call. _push_outbox_enqueue ist in JEDEM Test
gemockt — es wird nie ein Push an echte User gesendet. Der Sende-Fanout ist
zusätzlich hinter dem Flag HANGOUT_GEO_PUSH (Default AUS).
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


@pytest.fixture(autouse=True)
def _clear_cooldown():
    A._hangout_geo_push_last.clear()
    yield
    A._hangout_geo_push_last.clear()


# Referenz-Koordinaten (aus airports_compact.json, deterministisch).
FRA = 'FRA'   # Frankfurt
MUC = 'MUC'   # München (~300 km von FRA — ausserhalb 100 km)
CDG = 'CDG'   # Paris (weit weg)


def _coord(iata):
    ap = A._airports_compact_lookup()
    c = ap.get(iata)
    assert c, f'{iata} fehlt in airports_compact.json'
    return c[0], c[1]


class _FakeTokenTable:
    """Minimaler Fake für sb.table('user_push_tokens').select(...).range(...)."""

    def __init__(self, tokens):
        self._tokens = tokens

    def select(self, *a, **k):
        return self

    def range(self, lo, hi):
        self._lo, self._hi = lo, hi
        return self

    def execute(self):
        class _R:
            pass
        r = _R()
        r.data = [{'user_token': t} for t in self._tokens[self._lo:self._hi + 1]]
        return r


def _patch_sb(tokens):
    fake = _FakeTokenTable(tokens)

    def _table(name):
        assert name == 'user_push_tokens'
        return fake
    return patch.object(A, 'sb', type('SB', (), {'table': staticmethod(_table)})())


# ── _users_near: reine Geo-Selektion ────────────────────────────────────────

def test_users_near_finds_within_radius_excludes_far_and_creator():
    """User am selben Airport (FRA) ist drin; MUC (~300 km) ist draussen; der
    Ersteller wird ausgeschlossen."""
    lat, lon = _coord(FRA)
    tokens = ['AT-CREATOR', 'AT-NEAR', 'AT-FAR']
    profs = {
        'AT-CREATOR': {},
        'AT-NEAR': {},
        'AT-FAR': {},
    }
    iatas = {'AT-CREATOR': FRA, 'AT-NEAR': FRA, 'AT-FAR': MUC}
    with _patch_sb(tokens), \
         patch.object(A, 'SB_AVAILABLE', True), \
         patch.object(A, '_profiles_load_bulk', return_value=profs), \
         patch.object(A, '_user_current_iata', side_effect=lambda t: iatas.get(t)):
        near = A._users_near(lat, lon, exclude_token='AT-CREATOR')
    toks = {u['token'] for u in near}
    assert toks == {'AT-NEAR'}


def test_users_near_excludes_no_location():
    """User ohne heutigen Roster-Ort (kein IATA) hat keine bekannte Position."""
    lat, lon = _coord(FRA)
    tokens = ['AT-NEAR', 'AT-NOLOC']
    with _patch_sb(tokens), \
         patch.object(A, 'SB_AVAILABLE', True), \
         patch.object(A, '_profiles_load_bulk', return_value={'AT-NEAR': {}, 'AT-NOLOC': {}}), \
         patch.object(A, '_user_current_iata',
                      side_effect=lambda t: FRA if t == 'AT-NEAR' else None):
        near = A._users_near(lat, lon)
    assert {u['token'] for u in near} == {'AT-NEAR'}


def test_users_near_respects_share_location_false():
    """share_location == False → ausgeschlossen (Default True bleibt drin)."""
    lat, lon = _coord(FRA)
    tokens = ['AT-SHARE-ON', 'AT-SHARE-OFF', 'AT-DEFAULT']
    profs = {
        'AT-SHARE-ON': {'share_location': True},
        'AT-SHARE-OFF': {'share_location': False},
        'AT-DEFAULT': {},  # kein Key → Default an
    }
    with _patch_sb(tokens), \
         patch.object(A, 'SB_AVAILABLE', True), \
         patch.object(A, '_profiles_load_bulk', return_value=profs), \
         patch.object(A, '_user_current_iata', return_value=FRA):
        near = A._users_near(lat, lon)
    assert {u['token'] for u in near} == {'AT-SHARE-ON', 'AT-DEFAULT'}


def test_users_near_excludes_family_accounts():
    """Family-Konten (account_type == 'family') bekommen keinen Geo-Push."""
    lat, lon = _coord(FRA)
    tokens = ['AT-CREW', 'AT-FAMILY']
    profs = {
        'AT-CREW': {},
        'AT-FAMILY': {'account_type': 'family'},
    }
    with _patch_sb(tokens), \
         patch.object(A, 'SB_AVAILABLE', True), \
         patch.object(A, '_profiles_load_bulk', return_value=profs), \
         patch.object(A, '_user_current_iata', return_value=FRA):
        near = A._users_near(lat, lon)
    assert {u['token'] for u in near} == {'AT-CREW'}


def test_haversine_edge_distance_boundary():
    """Ein Ort knapp innerhalb / knapp ausserhalb des Radius via echte
    Haversine-Distanz zwischen zwei bekannten Airports."""
    flat, flon = _coord(FRA)
    mlat, mlon = _coord(MUC)
    d = A._haversine_km(flat, flon, mlat, mlon)
    # FRA↔MUC ist ~300 km — deutlich > 100 (Radius) und > 0.
    assert d > 250
    # Radius exakt auf d gesetzt ⇒ MUC-User wird gerade noch erfasst.
    tokens = ['AT-MUC']
    with _patch_sb(tokens), \
         patch.object(A, 'SB_AVAILABLE', True), \
         patch.object(A, '_profiles_load_bulk', return_value={'AT-MUC': {}}), \
         patch.object(A, '_user_current_iata', return_value=MUC):
        assert A._users_near(flat, flon, radius_km=d + 1)   # innerhalb
        assert not A._users_near(flat, flon, radius_km=d - 1)  # ausserhalb


# ── _hangout_notify_nearby: Deckel / Cooldown / Flag ────────────────────────

def _many_near(n):
    """n Empfänger-Dicts (wie _users_near sie liefert)."""
    return [{'token': f'AT-U{i}', 'iata': FRA, 'distance_km': 1.0}
            for i in range(n)]


def test_recipient_cap_enforced():
    """Über dem Deckel (MAX_RECIPIENTS) wird gekappt — nur N Sends."""
    n = A.HANGOUT_GEO_PUSH_MAX_RECIPIENTS + 50
    sent = []
    with patch.object(A, '_hangout_geo_push_enabled', return_value=True), \
         patch.object(A, '_users_near', return_value=_many_near(n)), \
         patch.object(A, '_push_outbox_enqueue',
                      side_effect=lambda tok, *a, **k: sent.append(tok)):
        res = A._hangout_notify_nearby('AT-CREATOR', 50.0, 8.5, FRA,
                                       title='Bier', pin_id='pin1')
    assert res['capped'] is True
    assert res['selected'] == n
    assert res['sent'] == A.HANGOUT_GEO_PUSH_MAX_RECIPIENTS
    assert len(sent) == A.HANGOUT_GEO_PUSH_MAX_RECIPIENTS


def test_creator_cooldown_blocks_second_push():
    """Zweiter Hangout desselben Erstellers innerhalb 30 min ⇒ kein Send."""
    sent = []
    with patch.object(A, '_hangout_geo_push_enabled', return_value=True), \
         patch.object(A, '_users_near', return_value=_many_near(3)), \
         patch.object(A, '_push_outbox_enqueue',
                      side_effect=lambda tok, *a, **k: sent.append(tok)):
        r1 = A._hangout_notify_nearby('AT-CREATOR', 50.0, 8.5, FRA,
                                      title='A', pin_id='p1', now_epoch=1000.0)
        r2 = A._hangout_notify_nearby('AT-CREATOR', 50.0, 8.5, FRA,
                                      title='B', pin_id='p2', now_epoch=1000.0 + 60)
    assert r1['sent'] == 3
    assert r2['cooldown'] is True
    assert r2['sent'] == 0
    assert len(sent) == 3  # nur der erste Hangout hat gesendet


def test_cooldown_expires_after_window():
    """Nach Ablauf des Cooldown-Fensters darf wieder gesendet werden."""
    sent = []
    with patch.object(A, '_hangout_geo_push_enabled', return_value=True), \
         patch.object(A, '_users_near', return_value=_many_near(2)), \
         patch.object(A, '_push_outbox_enqueue',
                      side_effect=lambda tok, *a, **k: sent.append(tok)):
        A._hangout_notify_nearby('AT-CREATOR', 50.0, 8.5, FRA,
                                 title='A', pin_id='p1', now_epoch=1000.0)
        later = 1000.0 + A.HANGOUT_GEO_PUSH_COOLDOWN_SEC + 1
        r2 = A._hangout_notify_nearby('AT-CREATOR', 50.0, 8.5, FRA,
                                      title='B', pin_id='p2', now_epoch=later)
    assert r2['cooldown'] is False
    assert r2['sent'] == 2
    assert len(sent) == 4


def test_flag_off_sends_nothing_only_counts():
    """Flag AUS ⇒ 0 echte Sends, nur count (selected>0)."""
    sent = []
    with patch.object(A, '_hangout_geo_push_enabled', return_value=False), \
         patch.object(A, '_users_near', return_value=_many_near(5)), \
         patch.object(A, '_push_outbox_enqueue',
                      side_effect=lambda tok, *a, **k: sent.append(tok)):
        res = A._hangout_notify_nearby('AT-CREATOR', 50.0, 8.5, FRA,
                                       title='X', pin_id='p1')
    assert res['enabled'] is False
    assert res['selected'] == 5
    assert res['sent'] == 0
    assert sent == []            # KEIN echter Send
    # Cooldown wird bei Flag-AUS NICHT gesetzt (nur Zählung).
    assert 'AT-CREATOR' not in A._hangout_geo_push_last


def test_flag_on_sends_to_expected_list():
    """Flag AN + gemockter Sender ⇒ genau N Sends an die erwartete Empfängerliste,
    Body enthält Ort + Titel, Deep-Link auf den Hangout."""
    near = [{'token': 'AT-A', 'iata': FRA, 'distance_km': 1.0},
            {'token': 'AT-B', 'iata': FRA, 'distance_km': 2.0}]
    calls = []

    def _capture(tok, title, body, data=None, **k):
        calls.append({'token': tok, 'title': title, 'body': body, 'data': data})

    with patch.object(A, '_hangout_geo_push_enabled', return_value=True), \
         patch.object(A, '_users_near', return_value=near), \
         patch.object(A, '_push_outbox_enqueue', side_effect=_capture):
        res = A._hangout_notify_nearby('AT-CREATOR', 50.0, 8.5, FRA,
                                       title='Feierabendbier', pin_id='pinXY')
    assert res['sent'] == 2
    assert [c['token'] for c in calls] == ['AT-A', 'AT-B']
    for c in calls:
        assert 'FRA' in c['body']
        assert 'Feierabendbier' in c['body']
        assert c['data']['type'] == 'hangout_nearby'
        assert c['data']['pin_id'] == 'pinXY'
        assert 'hangout' in c['data']['deep_link']


def test_notify_never_raises_on_internal_error():
    """Interner Fehler (z.B. _users_near wirft) bricht den Erstell-Response NICHT."""
    with patch.object(A, '_users_near', side_effect=RuntimeError('boom')):
        res = A._hangout_notify_nearby('AT-CREATOR', 50.0, 8.5, FRA,
                                       title='X', pin_id='p1')
    assert res['sent'] == 0
