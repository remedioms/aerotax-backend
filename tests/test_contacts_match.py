"""Kontakte-Matching (B1 Tibor 2026-07-12) — /api/user/contacts-match.

Warum es diesen Test gibt: Tibors Kontakte-Tab meldete „Von 265 Kontakten ist
(noch) niemand bei AeroX", obwohl Miguel & Jennifer (beide AeroX-User) in
seinen Kontakten standen. Root-Cause war das clientseitige Matching (60
ZUFÄLLIGE Namen via Set-Order, einzeln gegen /api/user/search). Der neue
Endpoint matcht serverseitig ALLE Kontakte in einem Request — über E-Mail-
Hashes (SHA-256, Registrierungs-E-Mail existiert für jeden Account) und
Namen. Diese Tests pinnen Matching-Regeln + Privacy-Verhalten.
"""
import hashlib
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app as A


def _h(email):
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


# ── Pure Helper: Namens-Normalisierung + Match-Regeln ────────────────────────

def test_name_tokens_strips_titles_emojis_punctuation():
    assert A._contacts_name_tokens('Dr. Miguel Schumann ✈️') == \
        ['dr', 'miguel', 'schumann']
    assert A._contacts_name_tokens('  Schumann,   Miguel ') == \
        ['schumann', 'miguel']
    assert A._contacts_name_tokens('Anna-Lena Müller') == \
        ['anna', 'lena', 'müller']
    assert A._contacts_name_tokens('✈️🛫') == []
    assert A._contacts_name_tokens(None) == []


def test_name_match_exact_and_reversed():
    contacts = [set(A._contacts_name_tokens('Schumann, Miguel'))]
    assert A._contacts_name_match('Miguel Schumann', contacts)


def test_name_match_profile_subset_of_contact():
    # Kontakt trägt Titel/Zusätze — Profilname (≥2 Tokens) steckt komplett drin
    contacts = [set(A._contacts_name_tokens('Dr. Miguel Schumann LH ✈️'))]
    assert A._contacts_name_match('Miguel Schumann', contacts)


def test_name_match_single_word_profile_only_exact():
    # Ein-Wort-Profilname darf NICHT jeden Kontakt mit dem Wort matchen
    contacts = [set(A._contacts_name_tokens('Miguel Schumann'))]
    assert not A._contacts_name_match('Miguel', contacts)
    assert A._contacts_name_match('Miguel', [{'miguel'}])


def test_name_match_no_substring_guessing():
    # „Jen" (Kontakt-Kürzel) matcht NICHT „Jennifer Orhan" — kein Substring
    contacts = [set(A._contacts_name_tokens('Jen'))]
    assert not A._contacts_name_match('Jennifer Orhan', contacts)


def test_email_hash_canonical():
    # Kanonisierung strip+lower — MUSS mit iOS (CrewConnectSearchView) identisch sein
    assert A._contacts_email_hash('  Miguel.Schumann@ICLOUD.com ') == \
        _h('miguel.schumann@icloud.com')


# ── Endpoint-Verhalten (SB weggemockt, Disk-Pfade gepatcht) ──────────────────

_AUTH_ROWS = [('miguel@icloud.com', 'AT-MIGUEL'),
              ('jennifer@web.de', 'AT-JENNIFER'),
              ('tibor@gmx.de', 'AT-TIBOR')]
_PROFILE_ROWS = [('AT-MIGUEL', 'Miguel Schumann'),
                 ('AT-JENNIFER', 'Jennifer Orhan'),
                 ('AT-ZOE', 'Zoe')]
_PROFILES = {
    'AT-MIGUEL': {'name': 'Miguel Schumann', 'homebase': 'FRA',
                  'airline': 'Lufthansa', 'position': 'FA',
                  'avatar_url': '/api/user/avatar/x/m.jpg'},
    'AT-JENNIFER': {'name': 'Jennifer Orhan', 'homebase': 'FRA',
                    'airline': 'Lufthansa', 'position': 'FA'},
    'AT-ZOE': {'name': 'Zoe', 'homebase': 'MUC'},
}


def _call(body, blocked=None):
    with patch.object(A, '_validate_token_exists', return_value='tibor@gmx.de'), \
         patch.object(A, '_token_rate_limited', return_value=False), \
         patch.object(A, 'SB_AVAILABLE', False), \
         patch.object(A, '_contacts_match_auth_rows', return_value=_AUTH_ROWS), \
         patch.object(A, '_contacts_match_profile_rows', return_value=_PROFILE_ROWS), \
         patch.object(A, '_blocked_by', return_value=(blocked or set())), \
         patch.object(A, '_profile_load',
                      side_effect=lambda t: {'token': t,
                                             'profile': _PROFILES.get(t, {})}):
        client = A.app.test_client()
        return client.post('/api/user/contacts-match', json=body)


def test_endpoint_matches_by_email_hash():
    r = _call({'token': 'AT-TIBOR',
               'email_hashes': [_h('Miguel@icloud.com')]})
    assert r.status_code == 200
    data = r.get_json()
    assert data['ok'] and data['count'] == 1
    u = data['users'][0]
    assert u['token'] == 'AT-MIGUEL'
    assert u['matched_by'] == 'email'
    # Privacy: NIE die E-Mail selbst in der Response
    assert 'email' not in u


def test_endpoint_matches_by_name_all_contacts_one_request():
    # 265-Kontakte-Szenario: ALLE Namen in einem Request, kein Sampling
    names = [f'Kontakt Nr{i}' for i in range(260)]
    names += ['Dr. Miguel Schumann ✈️', 'Jennifer Orhan']
    r = _call({'token': 'AT-TIBOR', 'names': names})
    data = r.get_json()
    toks = {u['token'] for u in data['users']}
    assert toks == {'AT-MIGUEL', 'AT-JENNIFER'}
    assert all(u['matched_by'] == 'name' for u in data['users'])
    assert data['checked']['names'] == 262


def test_endpoint_excludes_self_and_blocked():
    r = _call({'token': 'AT-TIBOR',
               'email_hashes': [_h('tibor@gmx.de'), _h('miguel@icloud.com'),
                                _h('jennifer@web.de')]},
              blocked={'AT-JENNIFER'})
    data = r.get_json()
    toks = {u['token'] for u in data['users']}
    assert 'AT-TIBOR' not in toks        # nie sich selbst
    assert 'AT-JENNIFER' not in toks     # geblockt
    assert toks == {'AT-MIGUEL'}


def test_endpoint_empty_basis_returns_zero_not_error():
    r = _call({'token': 'AT-TIBOR', 'names': ['✈️'], 'email_hashes': ['nope']})
    data = r.get_json()
    assert r.status_code == 200 and data['ok'] and data['count'] == 0


def test_endpoint_invalid_token_401():
    with patch.object(A, '_validate_token_exists', return_value=None):
        client = A.app.test_client()
        r = client.post('/api/user/contacts-match',
                        json={'token': 'AT-FAKE', 'names': ['Miguel Schumann']})
    assert r.status_code == 401


def test_endpoint_bad_hashes_ignored():
    # Nicht-Hex/falsche Länge wird verworfen statt zu crashen
    r = _call({'token': 'AT-TIBOR',
               'email_hashes': ['zz', None, 123, 'A' * 64,
                                _h('jennifer@web.de')]})
    data = r.get_json()
    assert {u['token'] for u in data['users']} == {'AT-JENNIFER'}
