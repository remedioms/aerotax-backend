"""Echter Name im Moderations-Panel bei anonymen Meldungen (Owner 2026-08-01).

Anonyme Wall-Posts/Forum-Threads/-Replies/Wall-Kommentare speichern bewusst
KEINEN Autoren-Snapshot (create_wall_post etc., „Anonymität ist ein
Produkt-Feature") — das Moderations-Panel zeigte deshalb bei einer Meldung
nur den nackten Token statt eines Namens.

`_resolve_reported_content` löst jetzt bei leerem `author_name` UND
`is_anonymous=True` den echten Profilnamen über `_admin_resolve_anon_author_name`
nach — AUSSCHLIESSLICH für den Admin-/Moderations-Pfad
(`_send_report_email_notification` + `admin_moderate_panel`). Das Panel
kennzeichnet den Fund als „(anonym gepostet)".

HARTE GRENZE: diese Auflösung darf NIE eine Public-API-Antwort erreichen —
die Anonymität nach außen bleibt unangetastet. Das wird hier per AST-Scan der
tatsächlichen Aufrufstellen UND per Response-Shape-Test der öffentlichen
Report-Route abgesichert.
"""
import ast
import inspect
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A


TOKEN = 'AT-ANONAUTHOR000001'


# ── 1. _admin_resolve_anon_author_name ─────────────────────────────────────

def test_resolve_anon_author_name_liefert_echten_namen():
    with patch.object(A, '_profile_load',
                      return_value={'profile': {'name': 'Max Mustermann'}}):
        assert A._admin_resolve_anon_author_name(TOKEN) == 'Max Mustermann'


def test_resolve_anon_author_name_leerer_name_liefert_none():
    with patch.object(A, '_profile_load', return_value={'profile': {'name': '   '}}):
        assert A._admin_resolve_anon_author_name(TOKEN) is None


def test_resolve_anon_author_name_fehler_liefert_none_statt_wurf():
    with patch.object(A, '_profile_load', side_effect=RuntimeError('boom')):
        assert A._admin_resolve_anon_author_name(TOKEN) is None


# ── 2. _resolve_reported_content: Nachauflösung nur bei is_anonymous ───────

def test_anonymer_wall_post_bekommt_echten_namen_und_anon_flag():
    post = {'id': 'p1', 'text': 'anonymer Beitrag', 'author_token': TOKEN,
            'author_name': None, 'author_short': None, 'is_anonymous': True}
    with patch.object(A, '_wall_load_posts', return_value=[post]), \
         patch.object(A, '_admin_resolve_anon_author_name',
                      return_value='Max Mustermann') as resolver:
        out = A._resolve_reported_content('wall_post', 'p1')
    assert out['author_name'] == 'Max Mustermann'
    assert out['is_anonymous'] is True
    assert out['author_token'] == TOKEN
    resolver.assert_called_once_with(TOKEN)


def test_nicht_anonymer_wall_post_wird_nicht_nachaufgeloest():
    """Ein normaler Post trägt schon einen Namen — der Resolver wird gar
    nicht erst gerufen (und darf auch nicht, sonst würde ein legitim leerer
    Name bei einem NICHT-anonymen Post fälschlich als "anonym" behandelt)."""
    post = {'id': 'p2', 'text': 'normaler Beitrag', 'author_token': TOKEN,
            'author_name': 'Erika Musterfrau', 'is_anonymous': False}
    with patch.object(A, '_wall_load_posts', return_value=[post]), \
         patch.object(A, '_admin_resolve_anon_author_name') as resolver:
        out = A._resolve_reported_content('wall_post', 'p2')
    assert out['author_name'] == 'Erika Musterfrau'
    assert out['is_anonymous'] is False
    resolver.assert_not_called()


def test_anonymer_post_ohne_aufloesbares_profil_bleibt_leer():
    post = {'id': 'p3', 'text': 'anonym, Profil weg', 'author_token': TOKEN,
            'author_name': None, 'author_short': None, 'is_anonymous': True}
    with patch.object(A, '_wall_load_posts', return_value=[post]), \
         patch.object(A, '_admin_resolve_anon_author_name', return_value=None):
        out = A._resolve_reported_content('wall_post', 'p3')
    assert out['author_name'] == ''
    assert out['is_anonymous'] is True


def test_anonymer_forum_thread_bekommt_echten_namen():
    thread = {'id': 't1', 'title': 'Titel', 'body': 'Body', 'author_token': TOKEN,
              'author_name': None, 'is_anonymous': True}
    with patch.object(A, '_forum_load_threads', return_value=[thread]), \
         patch.object(A, '_admin_resolve_anon_author_name', return_value='Til Schweiger'):
        out = A._resolve_reported_content('forum_thread', 't1')
    assert out['author_name'] == 'Til Schweiger'
    assert out['is_anonymous'] is True


def test_kein_treffer_liefert_none():
    with patch.object(A, '_wall_load_posts', return_value=[]):
        assert A._resolve_reported_content('wall_post', 'nichts') is None


# ── 3. Panel-Kennzeichnung „(anonym gepostet)" ─────────────────────────────

def _render_panel(resolved_content):
    report = {'id': 'r1', 'kind': 'wall_post', 'target_id': 'p1',
              'reason': 'spam', 'note': '', 'status': 'pending', 'ts': 0}
    with A.app.test_request_context('/api/admin/moderate'), \
         patch.object(A, '_admin_logged_in', return_value=True), \
         patch.object(A, '_load_reports', return_value=[report]), \
         patch.object(A, '_resolve_reported_content', return_value=resolved_content):
        resp = A.admin_moderate_panel()
    # admin_moderate_panel liefert rohes HTML (str) direkt, keinen Response.
    return resp if isinstance(resp, str) else resp.get_data(as_text=True)


def test_panel_kennzeichnet_nachaufgeloesten_anonymen_namen():
    html = _render_panel({'text': 'anonymer Beitrag', 'author_token': TOKEN,
                          'author_name': 'Max Mustermann', 'is_anonymous': True})
    assert 'Max Mustermann (anonym gepostet)' in html


def test_panel_zeigt_normalen_namen_ohne_anonym_zusatz():
    html = _render_panel({'text': 'normaler Beitrag', 'author_token': TOKEN,
                          'author_name': 'Erika Musterfrau', 'is_anonymous': False})
    assert 'Erika Musterfrau' in html
    assert '(anonym gepostet)' not in html


def test_panel_ohne_aufloesbaren_namen_zeigt_weiterhin_token():
    html = _render_panel({'text': 'anonym ohne Profil', 'author_token': TOKEN,
                          'author_name': '', 'is_anonymous': True})
    assert TOKEN in html
    assert '(anonym gepostet)' not in html


# ── 4. HARTE GRENZE: nur Admin-/Moderations-Pfad ───────────────────────────

_ADMIN_ONLY_CALLERS = {'_send_report_email_notification', 'admin_moderate_panel'}


def test_resolve_reported_content_wird_nur_aus_admin_pfaden_gerufen():
    """AST-Scan: JEDE Aufrufstelle von `_resolve_reported_content(` im
    Modul muss innerhalb einer der zwei bekannten Admin-Funktionen liegen.
    Ein neuer Aufrufer (z.B. ein zukünftiger Public-Endpoint) lässt diesen
    Test bewusst rot werden, statt den Leak still durchzulassen."""
    src = inspect.getsource(A)
    tree = ast.parse(src)

    callers = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Name)
                        and inner.func.id == '_resolve_reported_content'):
                    callers.append(node.name)

    assert callers, 'Sanity: mindestens ein Aufrufer sollte gefunden werden'
    unerwartet = sorted(set(callers) - _ADMIN_ONLY_CALLERS)
    assert not unerwartet, f'_resolve_reported_content wird auch aus {unerwartet} gerufen'


def test_public_report_endpoint_response_enthaelt_keine_autor_felder():
    """Der EINZIGE Public-Endpoint, der einen Report anlegt, gibt niemals
    author_token/author_name zurück — nur report_id + hidden-Flag."""
    with A.app.test_request_context(method='POST',
                                    json={'kind': 'wall_post', 'target_id': 'p1',
                                          'reason': 'spam'}), \
         patch.object(A, '_token_rate_limited', return_value=False), \
         patch.object(A, '_load_reports', return_value=[]), \
         patch.object(A, '_save_reports'), \
         patch.object(A, '_send_report_email_notification'):
        resp = A.moderation_report(TOKEN)
    body = resp.get_json()
    assert body['ok'] is True
    assert set(body.keys()) == {'ok', 'report_id', 'hidden'}
    assert 'author_name' not in body and 'author_token' not in body


def test_report_email_notification_ruft_resolver_nur_intern_ohne_response_leak():
    """Die Mail-Notification nutzt _resolve_reported_content intern (für den
    Betreiber), das ist erlaubt — aber die Funktion hat KEINEN Return-Wert,
    der irgendwo in eine HTTP-Antwort einfließen könnte."""
    entry = {'kind': 'wall_post', 'target_id': 'p1', 'reason': 'spam',
             'target_token': TOKEN, 'note': '', 'id': 'r1', 'reporter_token': TOKEN}
    sig = inspect.signature(A._send_report_email_notification)
    assert sig.return_annotation in (inspect.Signature.empty, None)
    with patch.object(A, '_resolve_reported_content',
                      return_value={'text': 'x', 'author_token': TOKEN,
                                    'author_name': 'Max Mustermann', 'is_anonymous': True}), \
         patch.dict(os.environ, {'RESEND_API_KEY': ''}):
        # RESEND_API_KEY fehlt in der Testumgebung → early-return vor jedem
        # Netzzugriff; hier zählt nur: kein Exception, kein Rückgabewert mit PII.
        result = A._send_report_email_notification(entry)
    assert result is None


def test_smp_meldung_speichert_frage_antwort_und_sendet_resend_notification():
    saved = []
    with A.app.test_request_context(method='POST', json={
        'kind': 'smp_flashcard', 'target_id': 'card-1', 'reason': 'wrong',
        'note': 'Die Definition ist fachlich nicht richtig.',
        'content_title': 'Was ist SMART?',
        'content_body': 'Schnell, modern, attraktiv.',
    }), patch.object(A, '_token_rate_limited', return_value=False), \
         patch.object(A, '_load_reports', return_value=[]), \
         patch.object(A, '_save_reports', side_effect=lambda rows: saved.extend(rows)), \
         patch.object(A, '_send_report_email_notification') as mail:
        resp = A.moderation_report(TOKEN)
    assert resp.status_code == 200
    assert saved[0]['content_title'] == 'Was ist SMART?'
    assert saved[0]['content_body'] == 'Schnell, modern, attraktiv.'
    mail.assert_called_once()


def test_zwei_unterschiedliche_smp_melder_blenden_community_karte_aus():
    existing = [{
        'id': 'r1', 'reporter_token': 'AT-FIRST0000000001',
        'kind': 'smp_flashcard', 'target_id': 'card-1',
        'reason': 'wrong', 'status': 'pending',
    }]
    with A.app.test_request_context(method='POST', json={
        'kind': 'smp_flashcard', 'target_id': 'card-1', 'reason': 'wrong',
        'note': 'Auch diese Antwort ist fachlich falsch.',
        'content_title': 'Frage', 'content_body': 'Antwort',
    }), patch.object(A, '_token_rate_limited', return_value=False), \
         patch.object(A, '_load_reports', return_value=existing), \
         patch.object(A, '_save_reports'), \
         patch.object(A, '_auto_hide_reported_smp_community_card',
                      return_value=True) as hide, \
         patch.object(A, '_send_report_email_notification') as mail:
        resp = A.moderation_report(TOKEN)
    assert resp.get_json()['hidden'] is True
    hide.assert_called_once_with('card-1')
    assert mail.call_args.kwargs == {'report_count': 2, 'auto_hidden': True}


def test_doppelte_smp_meldung_desselben_accounts_zaehlt_nur_einmal():
    existing = [{
        'id': 'r1', 'reporter_token': TOKEN,
        'kind': 'smp_flashcard', 'target_id': 'card-1',
        'reason': 'wrong', 'status': 'pending',
    }]
    with A.app.test_request_context(method='POST', json={
        'kind': 'smp_flashcard', 'target_id': 'card-1', 'reason': 'wrong',
        'note': 'Ich melde dieselbe Karte erneut.',
        'content_title': 'Frage', 'content_body': 'Antwort',
    }), patch.object(A, '_token_rate_limited', return_value=False), \
         patch.object(A, '_load_reports', return_value=existing), \
         patch.object(A, '_save_reports'), \
         patch.object(A, '_auto_hide_reported_smp_community_card') as hide, \
         patch.object(A, '_send_report_email_notification') as mail:
        resp = A.moderation_report(TOKEN)
    assert resp.get_json()['hidden'] is False
    hide.assert_not_called()
    assert mail.call_args.kwargs == {'report_count': 1, 'auto_hidden': False}


def test_smp_meldung_braucht_beschreibung():
    with A.app.test_request_context(method='POST', json={
        'kind': 'smp_exam_question', 'target_id': 'q-1', 'reason': 'wrong',
        'note': 'x', 'content_title': 'Frage', 'content_body': 'Antwort',
    }), patch.object(A, '_token_rate_limited', return_value=False):
        resp, status = A.moderation_report(TOKEN)
    assert status == 400
    assert resp.get_json()['error'] == 'description_required'


# ── Blocklisten-Leak (Codex-Schlusspruefung 13.08.) ─────────────────────────

def test_blocks_liste_liefert_keine_rohen_tokens():
    """GET /blocks gab `token` = das rohe Author-Credential zurueck. Ueber
    kind=news_comment haette man damit einen anonymen Kommentator
    deanonymisieren UND sein Bearer-Token abgreifen koennen. Jetzt: AXU-Ref."""
    viewer = 'AT-0123456789ABCDEF'
    blocked = 'AT-FEDCBA9876543210'
    _ok = A._TokenValidationResult(A._TokenValidationState.VALID, 'x@e.de')
    with patch.object(A, '_blocked_by', return_value={blocked}), \
         patch.object(A, '_validate_token', return_value=_ok), \
         patch.object(A, '_request_bearer_matches', return_value=True), \
         patch.object(A, '_user_profile_path', return_value='/nonexistent'):
        client = A.app.test_client()
        r = client.get(f'/api/moderation/{viewer}/blocks',
                       headers={'Authorization': 'Bearer ' + viewer})
    assert r.status_code == 200
    body = r.get_json()
    blob = str(body)
    assert blocked not in blob, 'rohes Author-Token in der Blocklisten-Antwort!'
    assert 'token' not in body['blocks'][0], 'Feld token darf nicht existieren'
    ref = body['blocks'][0].get('ref')
    assert ref and ref != blocked
    # Die Ref muss serverseitig zurueck aufloesen (fuers Entblocken).
    assert A._token_from_public_user_ref(ref) == blocked


def test_block_by_content_lehnt_news_comment_ab():
    """Codex-Zweitpass 13.08.: 'Autor blockieren' bei (evtl. anonymen)
    News-Kommentaren wuerde den Autor deanonymisierbar in die Blockliste
    legen. Der Endpoint lehnt kind=news_comment jetzt ab; Melden bleibt."""
    _ok = A._TokenValidationResult(A._TokenValidationState.VALID, 'x@e.de')
    with patch.object(A, '_validate_token', return_value=_ok), \
         patch.object(A, '_request_bearer_matches', return_value=True):
        client = A.app.test_client()
        r = client.post('/api/moderation/AT-0123456789ABCDEF/block-by-content',
                        json={'kind': 'news_comment', 'target_id': 'c1'},
                        headers={'Authorization': 'Bearer AT-0123456789ABCDEF'})
    assert r.status_code == 400
    assert r.get_json().get('error') == 'block_not_supported_for_kind'


# ── Moderations-Review-Befunde (13.08.) ─────────────────────────────────────

def test_block_by_content_wall_comment_loest_autor_auf():
    """Befund 1: die UI zeigte 'Autor blockieren' fuer wall_comment, aber
    block-by-content kannte den kind nicht -> author_not_found -> iOS fakte
    Erfolg. Jetzt loest der geteilte Resolver den Autor auf."""
    _ok = A._TokenValidationResult(A._TokenValidationState.VALID, 'x@e.de')
    saved = {}
    with patch.object(A, '_validate_token', return_value=_ok), \
         patch.object(A, '_request_bearer_matches', return_value=True), \
         patch.object(A, '_content_author_token', return_value='AT-AABBCCDDEEFF0011'), \
         patch.object(A, '_blocked_by', return_value=set()), \
         patch.object(A, '_save_set_file', side_effect=lambda p, s2: saved.update({'s': s2})), \
         patch.object(A, '_blocks_path', return_value='/tmp/b.json'):
        client = A.app.test_client()
        r = client.post('/api/moderation/AT-0123456789ABCDEF/block-by-content',
                        json={'kind': 'wall_comment', 'target_id': 'wc1'},
                        headers={'Authorization': 'Bearer AT-0123456789ABCDEF'})
    assert r.status_code == 200
    assert r.get_json()['ok'] is True
    assert 'AT-AABBCCDDEEFF0011' in saved['s']


def test_blocks_liste_unterdrueckt_guest_family_rohtoken():
    """Befund 3: _public_user_ref gibt Guest/Family-Token bytegleich roh
    zurueck -> der Rohtoken-Pfad war fuer diese Formen noch offen. Solche
    Eintraege werden aus der auslieferbaren Liste unterdrueckt."""
    _ok = A._TokenValidationResult(A._TokenValidationState.VALID, 'x@e.de')
    guest = 'AT-GUEST-abc123'
    with patch.object(A, '_validate_token', return_value=_ok), \
         patch.object(A, '_request_bearer_matches', return_value=True), \
         patch.object(A, '_blocked_by', return_value={guest}), \
         patch.object(A, '_user_profile_path', return_value='/nonexistent'):
        client = A.app.test_client()
        r = client.get('/api/moderation/AT-0123456789ABCDEF/blocks',
                       headers={'Authorization': 'Bearer AT-0123456789ABCDEF'})
    assert r.status_code == 200
    body = r.get_json()
    assert guest not in str(body)
    assert body['blocks'] == []
