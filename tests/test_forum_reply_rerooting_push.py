"""Regression 17.08.: Nach dem Re-Rooting ging der „auf deinen Kommentar
geantwortet"-Push an den FALSCHEN Empfaenger.

8229184 haengt eine Antwort auf eine Ebene-2-Reply automatisch an die WURZEL
ihrer Kette (das Forum bleibt einstufig). Dabei wird `parent_reply_id`
ueberschrieben. Der Push-Block liest den Parent danach ERNEUT ueber genau diese
ID — also die Wurzel. Ergebnis: der Wurzel-Autor bekam „hat auf deinen
Kommentar geantwortet", waehrend die Person, der wirklich geantwortet wurde,
nur den generischen Thread-Push erhielt. Genau die Luecke, die der Fix vom
24.07. (Florian) schliessen sollte.
"""

import os
from unittest.mock import patch

os.environ.setdefault("AEROTAX_ALLOW_BOOT_WITHOUT_KEY", "1")

import app as A

THREAD = 'thread-1'
ME = 'AT-1111111111111111'
ROOT_AUTHOR = 'AT-2222222222222222'
NESTED_AUTHOR = 'AT-3333333333333333'
THREAD_AUTHOR = 'AT-4444444444444444'

ROOT_REPLY = {'id': 'root1', 'thread_id': THREAD, 'parent_reply_id': None,
              'author_token': ROOT_AUTHOR}
NESTED_REPLY = {'id': 'nested1', 'thread_id': THREAD,
                'parent_reply_id': 'root1', 'author_token': NESTED_AUTHOR}
REPLIES = [ROOT_REPLY, NESTED_REPLY]


def _post(monkeypatch, parent_reply_id):
    """Antwort auf `parent_reply_id` absetzen, alle Pushes einsammeln."""
    pushes = []

    monkeypatch.setattr(A, '_token_rate_limited', lambda *a, **k: False)
    monkeypatch.setattr(
        A, '_forum_thread_sb_get',
        lambda tid: {'id': THREAD, 'author_token': THREAD_AUTHOR,
                     'category_id': 'c1'})
    monkeypatch.setattr(
        A, '_forum_reply_sb_get',
        lambda rid: next((r for r in REPLIES if r['id'] == rid), None))
    monkeypatch.setattr(A, '_forum_replies_load_from_disk',
                        lambda tid: list(REPLIES))
    monkeypatch.setattr(A, '_forum_load_replies', lambda tid: list(REPLIES))
    monkeypatch.setattr(A, '_forum_replies_save_to_supabase',
                        lambda tid, rows: None)
    monkeypatch.setattr(A, '_forum_disk_replies_mutate', lambda tid, fn: None)
    monkeypatch.setattr(A, '_forum_thread_apply_counters',
                        lambda *a, **k: True)
    monkeypatch.setattr(A, '_forum_author_snapshot',
                        lambda tok: {'author_name': 'Miguel'})
    monkeypatch.setattr(
        A, '_push_notify_async',
        lambda recipient, title, body, **kw: pushes.append(
            (recipient, kw.get('data', {}).get('title_localization_key'))))

    with A.app.test_request_context(
            f'/api/forum/{ME}/threads/{THREAD}/reply', method='POST',
            json={'body': 'Danke!', 'parent_reply_id': parent_reply_id}):
        response = A.app.make_response(A.forum_create_reply(ME, THREAD))
    assert response.status_code == 200, response.get_data(as_text=True)
    return pushes


def _replied_to_comment(pushes):
    return [recipient for recipient, key in pushes
            if key == 'push_title_replied_to_comment']


def test_reply_to_a_nested_reply_notifies_the_person_addressed(monkeypatch):
    pushes = _post(monkeypatch, 'nested1')
    # Die Antwort haengt technisch an der Wurzel — der „auf deinen Kommentar
    # geantwortet"-Push gehoert trotzdem dem, dem geantwortet wurde.
    assert _replied_to_comment(pushes) == [NESTED_AUTHOR]
    assert ROOT_AUTHOR not in _replied_to_comment(pushes)


def test_reply_to_a_root_reply_is_unchanged(monkeypatch):
    pushes = _post(monkeypatch, 'root1')
    assert _replied_to_comment(pushes) == [ROOT_AUTHOR]


def test_thread_author_still_gets_the_thread_push(monkeypatch):
    pushes = _post(monkeypatch, 'nested1')
    assert (THREAD_AUTHOR, 'push_title_replied') in pushes


def test_the_root_author_is_not_left_out_entirely(monkeypatch):
    # Er ist Mitdiskutierender und bekommt weiterhin den generischen
    # Thread-Push — nur eben nicht mehr den falschen.
    pushes = _post(monkeypatch, 'nested1')
    assert ROOT_AUTHOR in [recipient for recipient, _key in pushes]
