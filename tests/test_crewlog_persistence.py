"""Crew-Logbuch Privat-Persistenz (GET + POST /api/user/crewlog/<token>).

Privacy-Garantie: User A's Logbuch-Blob darf NIEMALS für User B zurückgegeben
werden. Jeder Token sieht ausschliesslich seinen eigenen Blob.

Tests:
  test_empty_logbook_returns_empty_people  — erstes GET liefert ok+[] (kein 404)
  test_roundtrip_save_and_load             — POST→GET round-trip (Inhalt erhalten)
  test_cross_user_isolation                — User A sieht NICHT den Blob von User B
  test_invalid_body_rejected               — POST ohne people-Liste → 400
  test_invalid_token_rejected              — leerer/fehlender Token → 400
"""
import os
import sys

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import app as A

TOKEN_A = 'AT-CREWLOG-TEST-USERA-001'
TOKEN_B = 'AT-CREWLOG-TEST-USERB-002'

SAMPLE_PEOPLE = [
    {
        'id': 'nico bauer',
        'name': 'Nico Bauer',
        'positions': ['CPT'],
        'encounters': [
            {'date': '2026-07-18', 'flight': 'LH400', 'dep': 'FRA', 'arr': 'JFK'}
        ],
        'note': 'Kaffee schwarz.',
    }
]


def _client():
    A.app.config.update(TESTING=True)
    return A.app.test_client()


def _clean(token):
    """Disk-Datei des Tokens vor Test löschen (Test-Isolation)."""
    import os
    try:
        p = A._crewlog_path(token)
        if p and os.path.exists(p):
            os.remove(p)
    except OSError:
        pass


def _patch_sb(monkeypatch):
    """SB-Calls + Token-Validation stumm schalten — Tests laufen gegen Disk-Tier.

    _validate_token wird so gepatcht, dass TEST-Tokens als VALID gelten.
    Identisches Pattern wie test_layover_rec_persistence.py."""
    monkeypatch.setattr(A, 'SB_AVAILABLE', False)
    monkeypatch.setattr(A, '_profile_load', lambda _t: {})
    monkeypatch.setattr(A, '_profile_save', lambda *_a, **_kw: True)
    monkeypatch.setattr(
        A, '_validate_token',
        lambda _token: A._TokenValidationResult(
            A._TokenValidationState.VALID, 'test@aerox.test'
        ),
    )


# ── Tests ────────────────────────────────────────────────────────────────────

def test_empty_logbook_returns_empty_people(monkeypatch):
    """Noch kein Logbuch gespeichert → ok=True, people=[]."""
    _patch_sb(monkeypatch)
    _clean(TOKEN_A)
    c = _client()
    r = c.get(f'/api/user/crewlog/{TOKEN_A}',
              headers={'Authorization': f'Bearer {TOKEN_A}'})
    assert r.status_code == 200, r.get_json()
    j = r.get_json()
    assert j['ok'] is True
    assert j['people'] == []


def test_roundtrip_save_and_load(monkeypatch):
    """POST speichert Blob; GET gibt denselben Inhalt zurück."""
    _patch_sb(monkeypatch)
    _clean(TOKEN_A)
    c = _client()
    # Speichern
    r_post = c.post(
        f'/api/user/crewlog/{TOKEN_A}',
        json={'people': SAMPLE_PEOPLE},
        headers={'Authorization': f'Bearer {TOKEN_A}'},
    )
    assert r_post.status_code == 200, r_post.get_json()
    assert r_post.get_json()['ok'] is True
    # Laden
    r_get = c.get(f'/api/user/crewlog/{TOKEN_A}',
                  headers={'Authorization': f'Bearer {TOKEN_A}'})
    assert r_get.status_code == 200, r_get.get_json()
    j = r_get.get_json()
    assert j['ok'] is True
    assert len(j['people']) == 1
    p = j['people'][0]
    assert p['id'] == 'nico bauer'
    assert p['note'] == 'Kaffee schwarz.'
    assert len(p['encounters']) == 1
    assert p['encounters'][0]['flight'] == 'LH400'


def test_cross_user_isolation(monkeypatch):
    """PRIVACY: User B darf NICHT den Blob von User A erhalten.

    Vorgehen: Token A speichert Daten → Token B liest → muss leer zurückkommen.
    Es darf unter keinen Umständen ein Blob eines anderen Tokens zurückgegeben
    werden — das wäre eine Datenpanne (User-A-Notizen sichtbar für User B)."""
    _patch_sb(monkeypatch)
    _clean(TOKEN_A)
    _clean(TOKEN_B)
    c = _client()
    # Token A speichert sein Logbuch
    r = c.post(
        f'/api/user/crewlog/{TOKEN_A}',
        json={'people': SAMPLE_PEOPLE},
        headers={'Authorization': f'Bearer {TOKEN_A}'},
    )
    assert r.status_code == 200
    # Token B liest — MUSS leer sein
    r_b = c.get(f'/api/user/crewlog/{TOKEN_B}',
                headers={'Authorization': f'Bearer {TOKEN_B}'})
    assert r_b.status_code == 200, r_b.get_json()
    j_b = r_b.get_json()
    assert j_b['ok'] is True
    # KERNAUSSAGE: Token B sieht KEINE Daten von Token A
    assert j_b['people'] == [], (
        'PRIVACY BREACH: Token B erhielt Daten von Token A!'
    )


def test_invalid_body_rejected(monkeypatch):
    """POST ohne people-Liste → 400 bad request."""
    _patch_sb(monkeypatch)
    c = _client()
    r = c.post(
        f'/api/user/crewlog/{TOKEN_A}',
        json={'wrong_key': 'foo'},
        headers={'Authorization': f'Bearer {TOKEN_A}'},
    )
    assert r.status_code == 400
    assert r.get_json()['ok'] is False
    assert r.get_json()['error'] == 'invalid_body'


def test_invalid_token_rejected(monkeypatch):
    """Leerer / ungültiger Token → 400."""
    _patch_sb(monkeypatch)
    c = _client()
    # Leerzeichen-Only Token (nach Sanitize leer)
    r = c.get('/api/user/crewlog/   ')
    # Flask routet einen Whitespace-Only-Slug normalerweise als 404 —
    # beides (400 oder 404) ist akzeptabel solange kein Blob zurückkommt.
    assert r.status_code in (400, 404)


def test_me_crewlog_uses_bearer_identity_not_a_url_credential(monkeypatch):
    """Android's additive endpoint persists under the validated bearer only."""
    _patch_sb(monkeypatch)
    _clean(TOKEN_A)
    c = _client()
    saved = c.post('/api/me/crewlog', json={'people': SAMPLE_PEOPLE},
                   headers={'Authorization': f'Bearer {TOKEN_A}'})
    assert saved.status_code == 200, saved.get_json()
    loaded = c.get('/api/me/crewlog',
                   headers={'Authorization': f'Bearer {TOKEN_A}'})
    assert loaded.status_code == 200, loaded.get_json()
    assert loaded.get_json()['people'][0]['id'] == 'nico bauer'


def test_me_crewlog_rejects_oversized_private_note(monkeypatch):
    _patch_sb(monkeypatch)
    c = _client()
    bad = [dict(SAMPLE_PEOPLE[0], note='x' * 2001)]
    response = c.post('/api/me/crewlog', json={'people': bad},
                      headers={'Authorization': f'Bearer {TOKEN_A}'})
    assert response.status_code == 400
    assert response.get_json()['error'] == 'invalid_body'
