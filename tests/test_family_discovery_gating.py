"""Family-Konten in Crew-Discovery: sichtbar-aber-markiert + Follow-Gate.

Owner-Entscheidung 2026-07-16 (Screenshot „Martina · Crew-Mitglied · [Folgen]"):
Family-Konten (Familien-Feature, für immer gratis, kein Dienstplan) bleiben in
der Crew-Suche/Discovery SICHTBAR, werden aber:
  1. additiv als role='family' markiert (der nächste iOS-Build zeigt daraus
     „Familie" statt „Crew-Mitglied" und blendet den Folgen-Button aus), und
  2. serverseitig NICHT per Crew-Follow verbindbar (Follow-Request AN ein
     Family-Konto → 4xx error='family_account_not_followable'). Alte iOS-Builds
     zeigen den Button noch → deshalb serverseitig hart abgelehnt.

Das Family-Pairing (family_scoped_tokens/Pair-Code, blueprints/family_watch.py
+ /api/family-share/…) ist ein ANDERER Pfad und darf NICHT brechen — der
Follow-Gate sitzt nur im Friend-Request-Core.

Marker = `account_type == 'family'` im Profil (einziger konsistent genutzter
Marker). FAIL-OPEN: unbekannt/fehlend ⇒ als CREW behandeln (keine echte Crew
fälschlich labeln/blocken).
"""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app as A


# ── Helper: _is_family_account (der geteilte Marker) ─────────────────────────

def test_is_family_account_marker():
    assert A._is_family_account({'account_type': 'family'}) is True
    assert A._is_family_account({'account_type': 'FAMILY'}) is True   # case-insensitiv
    assert A._is_family_account({'account_type': ' family '}) is True  # getrimmt
    # FAIL-OPEN: alles andere ist CREW (keine echte Crew fälschlich labeln)
    assert A._is_family_account({'account_type': 'crew'}) is False
    assert A._is_family_account({}) is False
    assert A._is_family_account({'account_type': None}) is False
    assert A._is_family_account(None) is False


# ── Suche: role='family' additiv, Crew byte-kompatibel (kein role) ───────────

_FAMILY_PROFILE = {
    'token': 'AT-MARTINA', 'profile': {
        'name': 'Martina Muster', 'homebase': 'FRA', 'airline': 'Lufthansa',
        'position': 'Family', 'account_type': 'family',
    }}
_CREW_PROFILE = {
    'token': 'AT-CREWMATE', 'profile': {
        'name': 'Martin Crew', 'homebase': 'FRA', 'airline': 'Lufthansa',
        'position': 'FA', 'account_type': 'crew',
    }}


def _write_disk_profiles(tmpdir, profiles):
    for p in profiles:
        with open(os.path.join(tmpdir, f"profile_{p['token']}.json"), 'w') as f:
            json.dump(p, f)


def _search(tmpdir, query, extra_qs=''):
    with patch.object(A, 'SB_AVAILABLE', False), \
         patch.object(A, '_USER_HISTORY_DIR', tmpdir), \
         patch.object(A, '_blocked_by', return_value=set()):
        client = A.app.test_client()
        return client.get(f'/api/user/search?q={query}&token=AT-SEARCHER{extra_qs}')


def test_search_family_gets_role_marker(tmp_path):
    d = str(tmp_path)
    _write_disk_profiles(d, [_FAMILY_PROFILE, _CREW_PROFILE])
    r = _search(d, 'Mart')
    assert r.status_code == 200
    users = {u['name']: u for u in r.get_json()['users']}
    # Family-Konto SICHTBAR (nicht ausgeblendet) + role='family' additiv
    assert 'Martina Muster' in users
    assert users['Martina Muster'].get('role') == 'family'
    assert users['Martina Muster'].get('account_type') == 'family'


def test_search_crew_has_no_role_field(tmp_path):
    # Golden byte-kompatibel: für Crew wird KEIN role-Feld gesetzt.
    d = str(tmp_path)
    _write_disk_profiles(d, [_FAMILY_PROFILE, _CREW_PROFILE])
    r = _search(d, 'Mart')
    users = {u['name']: u for u in r.get_json()['users']}
    assert 'Martin Crew' in users
    assert 'role' not in users['Martin Crew']


def test_search_exclude_family_optin_still_hides(tmp_path):
    # Opt-in bleibt Opt-in: exclude_family=1 blendet Family aus (unverändert).
    d = str(tmp_path)
    _write_disk_profiles(d, [_FAMILY_PROFILE, _CREW_PROFILE])
    r = _search(d, 'Mart', extra_qs='&exclude_family=1')
    names = {u['name'] for u in r.get_json()['users']}
    assert 'Martina Muster' not in names   # ausgeblendet
    assert 'Martin Crew' in names          # Crew bleibt


# ── Profil-Projektion: role='family' additiv, Crew unverändert ───────────────

def test_public_profile_projection_family_role():
    with patch.object(A, '_profile_load', return_value=_FAMILY_PROFILE):
        out = A._public_profile_projection('AT-MARTINA')
    assert out['profile'].get('role') == 'family'


def test_public_profile_projection_crew_no_role():
    with patch.object(A, '_profile_load', return_value=_CREW_PROFILE):
        out = A._public_profile_projection('AT-CREWMATE')
    assert 'role' not in out['profile']


# ── Follow-Gate: Follow AN Family → 4xx; Crew→Crew unverändert ok ────────────

def _profiles_by_token(token):
    return {'AT-MARTINA': _FAMILY_PROFILE,
            'AT-CREWMATE': _CREW_PROFILE}.get(token, {'token': token, 'profile': {}})


def test_follow_family_rejected_4xx():
    with A.app.app_context(), \
         patch.object(A, '_profile_load', side_effect=_profiles_by_token), \
         patch.object(A, '_token_rate_limited', return_value=False), \
         patch.object(A, '_blocked_by', return_value=set()):
        resp, status = A._send_friend_request_core('AT-CREWMATE', 'AT-MARTINA')
        body = resp.get_json()
    assert 400 <= status < 500
    assert body.get('ok') is False
    assert body.get('error') == 'family_account_not_followable'


def test_follow_crew_to_crew_not_family_gated():
    # Crew→Crew darf NICHT vom Family-Gate abgelehnt werden. Wir prüfen nur,
    # dass der Gate nicht greift (kein family_account_not_followable); die
    # tatsächliche Persistenz-Logik ist hier über die Mocks kurzgeschlossen.
    saved = {}

    def _fake_friends_load(t):
        return {'token': t, 'friends': [], 'requests_out': [], 'requests_in': []}

    with A.app.app_context(), \
         patch.object(A, '_profile_load', side_effect=_profiles_by_token), \
         patch.object(A, '_token_rate_limited', return_value=False), \
         patch.object(A, '_blocked_by', return_value=set()), \
         patch.object(A, 'SB_AVAILABLE', False), \
         patch.object(A, '_friends_load', side_effect=_fake_friends_load), \
         patch.object(A, '_friends_save',
                      side_effect=lambda t, v: saved.__setitem__(t, v)), \
         patch.object(A, '_push_notify_async', return_value=None):
        result = A._send_friend_request_core('AT-CREWMATE', 'AT-SOME-CREW',
                                             notify=False)
    # Kein (resp, status)-Tuple mit 4xx → Erfolg (jsonify-Response, 200-default).
    resp = result[0] if isinstance(result, tuple) else result
    body = resp.get_json()
    assert body.get('ok') is True
    assert body.get('error') != 'family_account_not_followable'


def test_family_pairing_path_untouched(tmp_path):
    # Family-Pairing (family-share grant/list) ist ein ANDERER Pfad und bleibt
    # funktionsfähig — der Follow-Gate berührt ihn nicht.
    import time
    import uuid
    client = A.app.test_client()
    email = f"famgate+{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}@aerox.test"
    r = client.post('/api/auth/signup', json={'email': email, 'password': 'Test12345!'})
    assert r.status_code == 200, r.get_data(as_text=True)
    token = r.get_json()['token']
    try:
        fam_token = 'AT-FAM-' + uuid.uuid4().hex[:8].upper()
        r = client.post(f'/api/family-share/{token}/grant',
                        json={'family_token': fam_token, 'relation': 'partner',
                              'fields': ['layover_place', 'current_city']})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.get_json().get('ok') is True
        r = client.get(f'/api/family-share/{token}/list')
        assert r.status_code == 200
        grants = r.get_json().get('grants') or []
        assert any(g.get('family_token') == fam_token for g in grants)
    finally:
        try:
            client.post('/api/auth/delete-account',
                        json={'email': email, 'password': 'Test12345!', 'token': token})
        except Exception:
            pass
