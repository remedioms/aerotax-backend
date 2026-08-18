"""Forum-Reply-Idempotenz (Owner 18.08.2026).

Retries nach Timeout/Netzwechsel erzeugten DOPPELTE Replies samt doppelter
Push-Welle. iOS schickt künftig pro Sende-Versuch eine stabile UUID als
`client_id` mit; der Server gibt bei (author_token, thread_id, client_id)-
Treffer die BESTEHENDE Reply zurück — kein zweiter Insert, keine Pushes.
Die client_id landet über den Unbekannte-Keys-Pfad des SB-Saves in der
metadata-jsonb-Spalte (voll aus dem Reply-Dict gebaut, nie partiell
überschrieben — Avatar/Roster-metadata-Clobber-Lektion).
"""

import os

os.environ.setdefault("AEROTAX_ALLOW_BOOT_WITHOUT_KEY", "1")

import app as A

THREAD = 'thread-idem-1'
ME = 'AT-1111111111111111'
THREAD_AUTHOR = 'AT-4444444444444444'


def _setup(monkeypatch):
    """In-Memory-Store statt SB/Disk: der Save persistiert, der Load liest —
    so sieht der zweite POST die Reply des ersten (wie in Prod)."""
    store = []
    pushes = []

    monkeypatch.setattr(A, '_token_rate_limited', lambda *a, **k: False)
    monkeypatch.setattr(
        A, '_forum_thread_sb_get',
        lambda tid: {'id': THREAD, 'author_token': THREAD_AUTHOR,
                     'category_id': 'c1'})
    monkeypatch.setattr(A, '_forum_reply_sb_get', lambda rid: None)
    monkeypatch.setattr(A, '_forum_replies_load_from_disk',
                        lambda tid: list(store))
    monkeypatch.setattr(A, '_forum_load_replies', lambda tid: list(store))
    monkeypatch.setattr(A, '_forum_replies_save_to_supabase',
                        lambda tid, rows: store.extend(rows) or True)
    monkeypatch.setattr(A, '_forum_disk_replies_mutate', lambda tid, fn: None)
    monkeypatch.setattr(A, '_forum_thread_apply_counters',
                        lambda *a, **k: True)
    monkeypatch.setattr(A, '_forum_author_snapshot',
                        lambda tok: {'author_name': 'Miguel'})
    monkeypatch.setattr(A, '_push_notify_async',
                        lambda recipient, title, body, **kw:
                        pushes.append(recipient))
    return store, pushes


def _post(payload):
    with A.app.test_request_context(
            f'/api/forum/{ME}/threads/{THREAD}/reply', method='POST',
            json=payload):
        response = A.app.make_response(A.forum_create_reply(ME, THREAD))
    return response.status_code, response.get_json()


def test_doppel_post_gleiche_client_id_eine_zeile(monkeypatch):
    store, pushes = _setup(monkeypatch)
    s1, b1 = _post({'body': 'Hallo!', 'client_id': 'uuid-abc-123'})
    assert s1 == 200 and b1['ok'] is True
    pushes_after_first = len(pushes)
    s2, b2 = _post({'body': 'Hallo!', 'client_id': 'uuid-abc-123'})
    assert s2 == 200 and b2['ok'] is True
    # GLEICHE id zurück, keine zweite Zeile, keine zweiten Pushes.
    assert b2['reply']['id'] == b1['reply']['id']
    assert b2.get('deduped') is True
    assert len(store) == 1
    assert len(pushes) == pushes_after_first


def test_client_id_wandert_in_den_persistierten_reply(monkeypatch):
    store, _ = _setup(monkeypatch)
    _post({'body': 'Hallo!', 'client_id': 'uuid-abc-123'})
    # Im Reply-Dict — der SB-Save sortiert unbekannte Keys nach metadata.
    assert store[0].get('client_id') == 'uuid-abc-123'


def test_verschiedene_client_ids_bleiben_zwei_replies(monkeypatch):
    store, _ = _setup(monkeypatch)
    _post({'body': 'Erste', 'client_id': 'uuid-1'})
    _post({'body': 'Zweite', 'client_id': 'uuid-2'})
    assert len(store) == 2


def test_dedupe_ist_pro_autor(monkeypatch):
    # Gleiche client_id von einem ANDEREN Autor darf nicht deduplizieren.
    store, _ = _setup(monkeypatch)
    _post({'body': 'Hallo!', 'client_id': 'uuid-abc-123'})
    other = 'AT-9999999999999999'
    with A.app.test_request_context(
            f'/api/forum/{other}/threads/{THREAD}/reply', method='POST',
            json={'body': 'Hallo!', 'client_id': 'uuid-abc-123'}):
        response = A.app.make_response(A.forum_create_reply(other, THREAD))
    assert response.status_code == 200
    assert len(store) == 2


def test_ohne_client_id_unveraendert_zwei_zeilen(monkeypatch):
    # Alte Clients (kein Feld) → exakt altes Verhalten, kein Dedupe.
    store, _ = _setup(monkeypatch)
    _post({'body': 'Hallo!'})
    _post({'body': 'Hallo!'})
    assert len(store) == 2


def test_client_id_zu_lang_400(monkeypatch):
    _setup(monkeypatch)
    s, b = _post({'body': 'Hallo!', 'client_id': 'x' * 65})
    assert s == 400 and b['error'] == 'invalid_client_id'


def test_client_id_falscher_typ_400(monkeypatch):
    _setup(monkeypatch)
    s, b = _post({'body': 'Hallo!', 'client_id': 123})
    assert s == 400 and b['error'] == 'invalid_body'
