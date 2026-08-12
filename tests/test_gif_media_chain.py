"""GIF-Medien-Kette (Owner-Go „max effort" 2026-08-12) — Server-Seite.

Der eine Satz, den diese Datei festnagelt:

    Ein animiertes GIF wird BYTE-IDENTISCH gespeichert und mit
    `Content-Type: image/gif` ausgeliefert — oder es wird mit klarem
    Fehlercode ABGELEHNT. Es wird NIE still umkodiert.

Warum das so scharf sein muss: eine Re-Kodierung (PIL → JPEG) macht aus einer
Animation ein Standbild. Der Nutzer sieht keinen Fehler, er sieht ein totes
Bild — genau die Klasse „Fehler darf nicht wie Leere aussehen" (Owner-Welle
08.08.). Deshalb prüfen die Tests hier die BYTES, nicht bloß den Statuscode.

Dazu die additiven Medien-Felder: `media_url` am News-Kommentar und
`image_url` an der Chat-Nachricht. Beide MÜSSEN für Alt-Clients folgenlos
sein — ein Request ohne das Feld verhält sich exakt wie vorher.
"""
import io
import os

import pytest

import app as A
import blueprints.news_blueprint as nb


TOKEN = 'AT-GIFCHAIN-0000000000000001'
NEWS_TOKEN = 'AT-' + '00A1B2C3D4E5F607'
ARTICLE = 'gifchain-artikel-aaa'
CHANNEL = 'group__gifchain'

_OWN_IMAGE = '/api/wall/image/0123456789abcdef0123456789abcdef/aabbccddeeff.gif'


# ── GIF-Bausteine ───────────────────────────────────────────────────────────

def _handcrafted_gif(width, height, padding=0):
    """Minimal gültiges GIF89a mit frei wählbarer Leinwandgröße.

    Genug für die Ablehn-Pfade: geprüft werden Magic-Bytes (Byte 0..6) und der
    Logical-Screen-Descriptor (Byte 6..10). `padding` bläht die Datei auf, um
    den 10-MB-Deckel zu testen, ohne ein echtes Riesen-GIF zu rendern.
    """
    head = (b'GIF89a'
            + bytes([width & 0xFF, (width >> 8) & 0xFF])
            + bytes([height & 0xFF, (height >> 8) & 0xFF])
            + b'\x80\x00\x00'
            + b'\x00\x00\x00\xff\xff\xff')          # 2-Farben-Palette
    body = (b'\x2c'                                  # Image Descriptor
            + b'\x00\x00\x00\x00'
            + bytes([width & 0xFF, (width >> 8) & 0xFF])
            + bytes([height & 0xFF, (height >> 8) & 0xFF])
            + b'\x00\x02\x02\x44\x01\x00')
    return head + body + (b'\x00' * padding) + b'\x3b'


def _animated_gif(width=64, height=64):
    """Echtes MEHRFRAMIGES GIF (PIL). Ohne PIL: handgebautes Einzelbild —
    die Byte-Identität lässt sich damit genauso beweisen."""
    if not A.PIL_AVAILABLE:
        return _handcrafted_gif(width, height), False
    from PIL import Image
    # Deutlich verschiedene Frames — sonst wirft der GIF-Writer sie als
    # Dublette weg und das Ergebnis wäre gar nicht animiert.
    frames = [Image.new('RGB', (width, height), c).convert('P')
              for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    buf = io.BytesIO()
    frames[0].save(buf, format='GIF', save_all=True,
                   append_images=frames[1:], duration=80, loop=0)
    return buf.getvalue(), True


def _jpeg_bytes(width=32, height=32):
    if not A.PIL_AVAILABLE:
        pytest.skip('PIL fehlt — JPEG-Vergleichspfad nicht baubar')
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (width, height), (10, 20, 30)).save(buf, format='JPEG')
    return buf.getvalue()


@pytest.fixture
def upload_env(tmp_path, monkeypatch):
    """Uploads landen im tmp-Verzeichnis, R2 aus, Rate-Limit offen."""
    monkeypatch.setattr(A, '_USER_HISTORY_DIR', str(tmp_path))
    monkeypatch.setattr(A, 'R2_AVATARS_ENABLED', False)
    monkeypatch.setattr(A, '_token_rate_limited', lambda *a, **k: False)
    return tmp_path


def _upload_wall(data, filename):
    with A.app.test_request_context(
            method='POST',
            data={'image': (io.BytesIO(data), filename)},
            content_type='multipart/form-data'):
        r = A.upload_wall_image(TOKEN)
    if isinstance(r, tuple):
        return r[0].get_json(), r[1]
    return r.get_json(), 200


def _serve_wall(url):
    """Holt die Bytes über den ÖFFENTLICHEN Serve-Pfad zurück."""
    _, _, _, _, dir_key, fname = url.split('/')
    with A.app.test_request_context():
        resp = A.serve_wall_image(dir_key, fname)
    resp.direct_passthrough = False
    return resp


# ── 1. GIF geht 1:1 durch ───────────────────────────────────────────────────

def test_gif_wird_byte_identisch_gespeichert_und_als_gif_ausgeliefert(
        upload_env):
    original, animated = _animated_gif()
    body, status = _upload_wall(original, 'lachen.gif')

    assert status == 200, body
    assert body['ok'] is True
    # Die Endung kommt aus dem Magic-Byte, nicht aus dem Dateinamen.
    assert body['url'].endswith('.gif')

    resp = _serve_wall(body['url'])
    served = resp.get_data()

    # DAS ist der Kern: kein Byte darf sich geändert haben.
    assert served == original
    assert served[:6] in (b'GIF87a', b'GIF89a')
    assert resp.mimetype == 'image/gif'

    if animated:
        from PIL import Image
        assert Image.open(io.BytesIO(served)).n_frames > 1, \
            'Animation überlebt den Upload nicht'


def test_gif_liegt_in_derselben_ablage_wie_fotos(upload_env):
    """Kein neuer Speicherort: GIF landet unter wall_images wie jedes Foto."""
    gif, _ = _animated_gif()
    body, _ = _upload_wall(gif, 'x.gif')
    jpg = _jpeg_bytes()
    body_jpg, _ = _upload_wall(jpg, 'x.jpg')

    gif_dir = os.path.dirname(body['url'])
    assert os.path.dirname(body_jpg['url']) == gif_dir
    stored = sorted(os.listdir(
        os.path.join(str(upload_env), 'wall_images', gif_dir.rsplit('/', 1)[-1])))
    assert len(stored) == 2


def test_gif_geht_auch_ueber_den_layover_pfad_durch(upload_env):
    gif, _ = _animated_gif()
    with A.app.test_request_context(
            method='POST',
            data={'image': (io.BytesIO(gif), 'tipp.gif')},
            content_type='multipart/form-data'):
        r = A.upload_layover_image(TOKEN)
    body = r.get_json() if not isinstance(r, tuple) else r[0].get_json()
    assert body['ok'] is True
    assert body['url'].endswith('.gif')

    _, _, _, _, dir_key, fname = body['url'].split('/')
    with A.app.test_request_context():
        resp = A.serve_layover_image(dir_key, fname)
    resp.direct_passthrough = False
    assert resp.get_data() == gif
    assert resp.mimetype == 'image/gif'


# ── 2. Deckel: ablehnen statt umkodieren ────────────────────────────────────

def test_zu_grosses_gif_wird_abgelehnt_nicht_umkodiert(upload_env):
    fett = _handcrafted_gif(200, 200, padding=10 * 1024 * 1024)
    assert len(fett) > 10 * 1024 * 1024
    body, status = _upload_wall(fett, 'fett.gif')

    assert status == 413
    assert body['error'] == 'gif_too_large_10mb'
    assert body['message']            # der Client kann das anzeigen
    # Nichts darf gespeichert worden sein.
    assert not os.path.isdir(os.path.join(str(upload_env), 'wall_images'))


def test_zu_breites_gif_wird_abgelehnt(upload_env):
    breit = _handcrafted_gif(1201, 400)
    body, status = _upload_wall(breit, 'breit.gif')

    assert status == 413
    assert body['error'] == 'gif_too_large_1200px'
    assert '1201' in body['message']
    assert not os.path.isdir(os.path.join(str(upload_env), 'wall_images'))


def test_gif_auf_der_kante_geht_noch_durch(upload_env):
    kante = _handcrafted_gif(1200, 1200)
    body, status = _upload_wall(kante, 'kante.gif')
    assert status == 200, body
    assert _serve_wall(body['url']).get_data() == kante


def test_kaputtes_gif_wird_abgelehnt(upload_env):
    body, status = _upload_wall(b'GIF89a' + b'\x00' * 40, 'kaputt.gif')
    assert status == 415
    assert body['error'] == 'invalid_gif'


# ── 3. Die statischen Formate bleiben, wie sie waren ────────────────────────

def test_jpeg_pfad_ist_unveraendert(upload_env):
    jpg = _jpeg_bytes()
    body, status = _upload_wall(jpg, 'foto.jpg')

    assert status == 200
    assert body['url'].endswith('.jpg')
    resp = _serve_wall(body['url'])
    assert resp.get_data() == jpg
    assert resp.mimetype == 'image/jpeg'


def test_zu_grosses_jpeg_bleibt_bei_too_large_5mb(upload_env):
    fett = _jpeg_bytes() + b'\x00' * (5 * 1024 * 1024)
    body, status = _upload_wall(fett, 'fett.jpg')
    assert status == 413
    assert body['error'] == 'too_large_5mb'


def test_fremdformat_bleibt_415(upload_env):
    body, status = _upload_wall(b'<html>' + b'x' * 100, 'boese.jpg')
    assert status == 415
    assert body['error'] == 'invalid_image'


def test_avatar_pfad_nimmt_weiterhin_kein_gif(upload_env, monkeypatch):
    """Bewusste Grenze: der Avatar-Serve kennt kein image/gif — dort bleibt
    GIF abgelehnt wie bisher, statt ein kaputt ausgeliefertes Foto zu bauen."""
    gif, _ = _animated_gif()
    with A.app.test_request_context(
            method='POST',
            data={'image': (io.BytesIO(gif), 'a.gif')},
            content_type='multipart/form-data'):
        r = A.upload_user_avatar(TOKEN)
    payload, status = r[0].get_json(), r[1]
    assert status == 415
    assert payload['error'] == 'invalid_image'


# ── 4. Chat-Nachricht mit Medium ────────────────────────────────────────────

def _send_chat(json_body, existing=None):
    from unittest.mock import patch
    with (
        A.app.test_request_context(method='POST', json=json_body),
        patch.object(A, '_chat_path', return_value='/tmp/gifchain.json'),
        patch.object(A, '_channel_access_error', return_value=None),
        patch.object(A, '_dm_load_messages', return_value=list(existing or [])),
        patch.object(A, '_dm_load_messages_from_disk', return_value=[]),
        patch.object(A, '_dm_messages_save_to_supabase', return_value=True),
        patch.object(A, '_dm_save_messages_disk', return_value=True),
        patch.object(A, '_chat_push_fanout_async') as push,
        patch.object(A, '_token_rate_limited', return_value=False),
        patch.object(A, '_profile_load', return_value={'profile': {}}),
    ):
        r = A.send_chat_message(TOKEN, CHANNEL)
        pushed = [c.args[2] for c in push.call_args_list]
    if isinstance(r, tuple):
        return r[0].get_json(), r[1], pushed
    return r.get_json(), 200, pushed


def test_chat_nachricht_traegt_das_gif_als_eigenes_feld():
    body, status, pushed = _send_chat({'text': '', 'image_url': _OWN_IMAGE})
    assert status == 200, body
    assert body['message']['image_url'] == _OWN_IMAGE
    assert body['message']['text'] == ''
    # Push-Vorschau darf nicht leer sein — die Endung sagt, was es ist.
    assert pushed == ['GIF']


def test_chat_nachricht_ohne_medium_ist_unveraendert():
    body, status, _ = _send_chat({'text': 'Hallo'})
    assert status == 200
    assert 'image_url' not in body['message']      # additiv: kein neuer Key
    assert body['message']['text'] == 'Hallo'


def test_chat_leere_nachricht_bleibt_empty_text():
    body, status, _ = _send_chat({'text': ''})
    assert status == 400
    assert body['error'] == 'empty_text'


def test_chat_fremde_bild_url_wird_verworfen():
    """Ein fremder Host im DM wäre ein Tracking-Beacon (Full-Review 01.08.)."""
    body, status, _ = _send_chat(
        {'text': '', 'image_url': 'https://tracker.example/x.gif'})
    assert status == 400
    assert body['error'] == 'empty_text'


def test_chat_vorschau_nennt_gif_und_foto():
    assert A._dm_preview_text({'image_url': _OWN_IMAGE}) == 'GIF'
    assert A._dm_preview_text({'image_url': '/api/wall/image/a/b.jpg'}) == 'Foto'
    assert A._dm_preview_text({'text': 'Text', 'image_url': _OWN_IMAGE}) == 'Text'
    assert A._dm_preview_text(None) == ''


# ── 5. News-Kommentar mit media_url ─────────────────────────────────────────

class _Result:
    def __init__(self, data):
        self.data = data


class _Table:
    """Nur die von news_create_comment/news_list_comments benutzten Aufrufe."""

    def __init__(self, rows):
        self.rows = rows
        self._in = None
        self._eqs = {}
        self._limit = None
        self.mode = None
        self.payload = None

    def select(self, *_a, **_k):
        self.mode = 'select'
        return self

    def in_(self, key, values):
        self._in = (key, list(values))
        return self

    def eq(self, key, value):
        self._eqs[key] = value
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, n):
        self._limit = n
        return self

    def lt(self, *_a, **_k):
        return self

    def insert(self, row):
        self.mode = 'insert'
        self.payload = dict(row)
        return self

    def execute(self):
        if self.mode == 'insert':
            # Spiegelt den NOT-NULL-Charakter der Tabelle: was nicht
            # mitgeschickt wird, existiert in der Zeile auch nicht.
            self.rows.append(dict(self.payload))
            return _Result([dict(self.payload)])
        hits = [dict(r) for r in self.rows
                if (not self._in or r.get(self._in[0]) in self._in[1])
                and all(r.get(k) == v for k, v in self._eqs.items())]
        hits.sort(key=lambda r: str(r.get('created_at') or ''), reverse=True)
        return _Result(hits[:self._limit] if self._limit else hits)


class _SB:
    def __init__(self):
        self.tables = {'ax_news_comments': []}

    def table(self, name):
        return _Table(self.tables.setdefault(name, []))


@pytest.fixture
def news_client(monkeypatch):
    from flask import Flask
    sb = _SB()
    monkeypatch.setattr(nb, '_news_authed_token', lambda: (NEWS_TOKEN, None))
    monkeypatch.setattr(nb, '_news_sb', lambda: (sb, True))
    monkeypatch.setattr(nb, '_news_rate_limited', lambda *a, **k: False)
    monkeypatch.setattr(nb, '_news_blocked_by', lambda _t: set())
    monkeypatch.setattr(nb, '_news_author_name', lambda t: 'Crew')
    monkeypatch.setattr(nb, '_news_story_ids', lambda aid: [aid])
    app = __import__('flask').Flask(__name__)
    app.register_blueprint(nb.news_bp)
    client = app.test_client()
    client._sb = sb
    assert Flask
    return client


def test_news_kommentar_mit_media_url_geht_rund(news_client):
    r = news_client.post(f'/api/news/{ARTICLE}/comments',
                         json={'body': 'Stark!', 'media_url': _OWN_IMAGE})
    assert r.status_code == 200, r.get_json()
    created = r.get_json()['comment']
    assert created['media_url'] == _OWN_IMAGE
    assert created['body'] == 'Stark!'

    g = news_client.get(f'/api/news/{ARTICLE}/comments')
    assert g.status_code == 200
    items = g.get_json()['items']
    assert len(items) == 1
    assert items[0]['media_url'] == _OWN_IMAGE
    assert items[0]['id'] == created['id']


def test_news_kommentar_nur_gif_ohne_text_ist_erlaubt(news_client):
    r = news_client.post(f'/api/news/{ARTICLE}/comments',
                         json={'media_url': _OWN_IMAGE})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['comment']['body'] == ''
    assert r.get_json()['comment']['media_url'] == _OWN_IMAGE


def test_news_kommentar_ohne_media_url_ist_unveraendert(news_client):
    """Alt-Client-Vertrag: der Key ist da, aber null — und die Zeile trägt
    KEINE media_url-Spalte (läuft damit auch vor der Migration)."""
    r = news_client.post(f'/api/news/{ARTICLE}/comments',
                         json={'body': 'Nur Text'})
    assert r.status_code == 200
    payload = r.get_json()['comment']
    assert payload['media_url'] is None
    assert payload['body'] == 'Nur Text'
    # Feld-Shape für iOS: alles Bisherige unverändert vorhanden.
    for key in ('id', 'article_id', 'author_public_ref', 'author_name',
                'body', 'created_at', 'is_mine'):
        assert key in payload
    assert 'media_url' not in news_client._sb.tables['ax_news_comments'][0]


def test_news_kommentar_ganz_leer_bleibt_abgelehnt(news_client):
    r = news_client.post(f'/api/news/{ARTICLE}/comments', json={'body': '  '})
    assert r.status_code == 400
    assert r.get_json()['error'] == 'empty_comment'


def test_news_kommentar_verwirft_fremde_media_url(news_client):
    r = news_client.post(
        f'/api/news/{ARTICLE}/comments',
        json={'body': 'Text', 'media_url': 'https://tracker.example/x.gif'})
    assert r.status_code == 200
    assert r.get_json()['comment']['media_url'] is None
    assert 'media_url' not in news_client._sb.tables['ax_news_comments'][0]

    # Und ohne Text ist eine fremde URL kein Kommentar.
    r2 = news_client.post(
        f'/api/news/{ARTICLE}/comments',
        json={'media_url': '//tracker.example/x.gif'})
    assert r2.status_code == 400
    assert r2.get_json()['error'] == 'empty_comment'


def test_news_kommentar_nimmt_auch_image_url_an(news_client):
    """Feldnamen-Brücke: die iOS-Composer-Runde schickt `image_url`, der
    Backend-Auftrag nannte `media_url`. Beide müssen ankommen, und beide
    Namen stehen in der Antwort — sonst entscheidet ein Tippfehler im
    Feldnamen darüber, ob das GIF ankommt."""
    r = news_client.post(f'/api/news/{ARTICLE}/comments',
                         json={'body': 'Mit Bild', 'image_url': _OWN_IMAGE})
    assert r.status_code == 200, r.get_json()
    created = r.get_json()['comment']
    assert created['media_url'] == _OWN_IMAGE
    assert created['image_url'] == _OWN_IMAGE

    items = news_client.get(f'/api/news/{ARTICLE}/comments').get_json()['items']
    assert items[0]['image_url'] == items[0]['media_url'] == _OWN_IMAGE


# ── 6. Anonyme News-Kommentare ──────────────────────────────────────────────

def test_anonymer_news_kommentar_geht_rund_ohne_ref(news_client):
    r = news_client.post(f'/api/news/{ARTICLE}/comments',
                         json={'body': 'Anonym gesagt', 'is_anonymous': True})
    assert r.status_code == 200, r.get_json()
    created = r.get_json()['comment']

    assert created['is_anonymous'] is True
    # KEINE AXU-Ref und KEIN Klarname nach außen …
    assert created['author_public_ref'] == ''
    assert created['author_name'] == created['anon_handle']
    assert created['anon_handle'] and created['anon_handle'] != 'Anonym'
    # … aber der eigene Kommentar bleibt für den Autor als solcher erkennbar.
    assert created['is_mine'] is True

    items = news_client.get(f'/api/news/{ARTICLE}/comments').get_json()['items']
    assert len(items) == 1
    assert items[0]['is_anonymous'] is True
    assert items[0]['author_public_ref'] == ''
    assert items[0]['anon_handle'] == created['anon_handle']
    # Der LIVE aufgelöste Klarname darf den Handle nicht überschreiben.
    assert items[0]['author_name'] == created['anon_handle']
    assert 'Crew' not in items[0]['author_name']


def test_anonymes_gif_ohne_text_geht(news_client):
    r = news_client.post(f'/api/news/{ARTICLE}/comments',
                         json={'media_url': _OWN_IMAGE, 'is_anonymous': True})
    assert r.status_code == 200, r.get_json()
    c = r.get_json()['comment']
    assert c['is_anonymous'] is True
    assert c['media_url'] == _OWN_IMAGE
    assert c['body'] == ''


def test_anonyme_kommentare_sind_untereinander_nicht_verkettbar(news_client):
    a = news_client.post(f'/api/news/{ARTICLE}/comments',
                         json={'body': 'eins', 'is_anonymous': True})
    b = news_client.post(f'/api/news/{ARTICLE}/comments',
                         json={'body': 'zwei', 'is_anonymous': True})
    ha = a.get_json()['comment']['anon_handle']
    hb = b.get_json()['comment']['anon_handle']
    assert ha and hb and ha != hb, 'Per-Kommentar-Salt fehlt'


def test_anonym_schuetzt_nicht_vor_moderation(news_client, monkeypatch):
    """Anonym heißt anonym gegenüber NUTZERN, nicht gegenüber der Moderation."""
    r = news_client.post(f'/api/news/{ARTICLE}/comments',
                         json={'body': 'Anonym', 'is_anonymous': True})
    cid = r.get_json()['comment']['id']
    row = news_client._sb.tables['ax_news_comments'][0]
    assert row['author_token'] == NEWS_TOKEN      # steht in der Zeile …
    assert row['is_anonymous'] is True

    # … und der Melde-/Block-Pfad löst ihn weiter auf.
    assert nb.news_comment_author_token(cid) == NEWS_TOKEN


def test_blockieren_trifft_auch_den_anonymen_autor(news_client, monkeypatch):
    news_client.post(f'/api/news/{ARTICLE}/comments',
                     json={'body': 'Anonym', 'is_anonymous': True})
    monkeypatch.setattr(nb, '_news_blocked_by', lambda _t: {NEWS_TOKEN})
    items = news_client.get(f'/api/news/{ARTICLE}/comments').get_json()['items']
    assert items == []


def test_alter_kommentar_ohne_flag_ist_unveraendert(news_client):
    r = news_client.post(f'/api/news/{ARTICLE}/comments', json={'body': 'Klar'})
    c = r.get_json()['comment']
    assert c['is_anonymous'] is False
    assert c['anon_handle'] is None
    assert c['author_public_ref']                 # echte AXU-Ref
    assert c['author_name'] == 'Crew'
    # Die Zeile trägt die neuen Spalten gar nicht (läuft vor der Migration).
    row = news_client._sb.tables['ax_news_comments'][0]
    assert 'is_anonymous' not in row
    assert 'anon_handle' not in row


# ── 7. GIF-Such-Proxy (anbieterneutral) ─────────────────────────────────────

import blueprints.gif_search_blueprint as gs   # noqa: E402


_PROVIDER_ANSWER = {
    'data': [
        {'id': 'abc123', 'images': {
            'original': {'url': 'https://media.giphy.com/media/abc123/giphy.gif?cid=x',
                         'width': '480', 'height': '270'},
            'fixed_width': {'url': 'https://media.giphy.com/media/abc123/200w.gif'},
        }},
        # Unvollständig → muss rausfliegen, statt eine tote Kachel zu zeigen.
        {'id': 'kaputt', 'images': {'original': {}}},
    ]
}


@pytest.fixture
def gif_client(monkeypatch):
    from flask import Flask
    gs.cache_clear()
    monkeypatch.setattr(gs, '_authed_token', lambda: (TOKEN, None))
    monkeypatch.setattr(gs, '_api_key', lambda: 'dummy-not-a-real-key')
    monkeypatch.setattr(gs, '_rate_limited', lambda *a, **k: False)
    app = Flask(__name__)
    app.register_blueprint(gs.gif_search_bp)
    yield app.test_client()
    gs.cache_clear()


def test_proxy_normalisiert_auf_unsere_shape(gif_client, monkeypatch):
    monkeypatch.setattr(gs, '_fetch_json', lambda _u: dict(_PROVIDER_ANSWER))
    r = gif_client.get(f'/api/gif-search/{TOKEN}?q=lachen')
    assert r.status_code == 200
    body = r.get_json()

    assert body['attribution'] == 'GIPHY'
    assert body['cached'] is False
    assert len(body['items']) == 1              # der kaputte Eintrag fiel raus
    item = body['items'][0]
    assert set(item) == {'id', 'preview_url', 'gif_url', 'width', 'height'}
    assert item['gif_url'] == 'https://media.giphy.com/media/abc123/giphy.gif'
    assert item['width'] == 480 and item['height'] == 270
    # Der Anbieter-Rohbau darf NICHT durchschlagen.
    assert 'data' not in body and 'images' not in item


def test_zweite_gleiche_suche_kommt_aus_dem_cache(gif_client, monkeypatch):
    calls = {'n': 0}

    def _fake(_u):
        calls['n'] += 1
        return dict(_PROVIDER_ANSWER)

    monkeypatch.setattr(gs, '_fetch_json', _fake)
    first = gif_client.get(f'/api/gif-search/{TOKEN}?q=lachen')
    second = gif_client.get(f'/api/gif-search/{TOKEN}?q=lachen')

    assert calls['n'] == 1, 'Cache-Treffer hat den Anbieter erneut gerufen'
    assert first.get_json()['cached'] is False
    assert second.get_json()['cached'] is True
    assert second.get_json()['items'] == first.get_json()['items']

    # Andere Suche → eigener Cache-Eintrag, also ein neuer Anbieter-Ruf.
    gif_client.get(f'/api/gif-search/{TOKEN}?q=winken')
    assert calls['n'] == 2


def test_trending_ohne_q_funktioniert(gif_client, monkeypatch):
    seen = {}

    def _fake(url):
        seen['url'] = url
        return dict(_PROVIDER_ANSWER)

    monkeypatch.setattr(gs, '_fetch_json', _fake)
    r = gif_client.get(f'/api/gif-search/{TOKEN}')
    assert r.status_code == 200
    assert '/trending?' in seen['url']
    assert len(r.get_json()['items']) == 1


def test_ohne_key_sagt_der_server_es_ehrlich(gif_client, monkeypatch):
    monkeypatch.setattr(gs, '_api_key', lambda: '')
    monkeypatch.setattr(gs, '_fetch_json',
                        lambda _u: pytest.fail('darf nicht gerufen werden'))
    r = gif_client.get(f'/api/gif-search/{TOKEN}?q=lachen')
    assert r.status_code == 503
    assert r.get_json()['error'] == 'gif_search_unavailable'


def test_ohne_bearer_gibt_es_keine_suche(monkeypatch):
    from flask import Flask, jsonify
    gs.cache_clear()
    monkeypatch.setattr(
        gs, '_authed_token',
        lambda: (None, (jsonify({'ok': False, 'error': 'auth_required'}), 401)))
    app = Flask(__name__)
    app.register_blueprint(gs.gif_search_bp)
    r = app.test_client().get(f'/api/gif-search/{TOKEN}?q=lachen')
    assert r.status_code == 401


def test_such_rate_limit_greift(gif_client, monkeypatch):
    monkeypatch.setattr(gs, '_rate_limited', lambda *a, **k: True)
    monkeypatch.setattr(gs, '_fetch_json',
                        lambda _u: pytest.fail('darf nicht gerufen werden'))
    r = gif_client.get(f'/api/gif-search/{TOKEN}?q=lachen')
    assert r.status_code == 429
    assert r.get_json()['error'] == 'rate_limited'


def test_der_key_steht_in_keiner_antwort(gif_client, monkeypatch):
    monkeypatch.setattr(gs, '_api_key', lambda: 'geheim-xyz')
    monkeypatch.setattr(gs, '_fetch_json', lambda _u: dict(_PROVIDER_ANSWER))
    r = gif_client.get(f'/api/gif-search/{TOKEN}?q=lachen')
    assert 'geheim-xyz' not in r.get_data(as_text=True)


# ── 8. Import: der Anbieter wird für ein GIF genau EINMAL gerufen ───────────

@pytest.fixture
def import_client(gif_client, upload_env, monkeypatch):
    """Import-Route + echte Wall-Ablage im tmp-Verzeichnis.

    `_app_attr` wird bewusst auf GENAU das app-Modul festgenagelt, das dieser
    Test gepatcht hat: `tests/test_calculation.py` tauscht `sys.modules['app']`
    aus, und der Blueprint löst `app` erst zur Laufzeit auf. Ohne diese Klammer
    schriebe der Import je nach Test-Reihenfolge in das ECHTE
    `_user_history_state`-Verzeichnis statt ins tmp.
    """
    monkeypatch.setattr(gs, '_app_attr',
                        lambda name, default=None: getattr(A, name, default))
    return gif_client


def _import(client, url, body=None):
    return client.post(f'/api/gif-search/{TOKEN}/import', json=body or {'gif_url': url})


def test_import_holt_das_gif_einmal_und_liefert_unseren_pfad(
        import_client, monkeypatch):
    gif, _ = _animated_gif()
    calls = {'n': 0}

    def _dl(_url):
        calls['n'] += 1
        return gif, None

    monkeypatch.setattr(gs, '_download_limited', _dl)
    r = _import(import_client, 'https://media.giphy.com/media/abc/giphy.gif')

    assert r.status_code == 200, r.get_json()
    url = r.get_json()['url']
    assert url.startswith('/api/wall/image/')      # zeigt auf UNS
    assert url.endswith('.gif')
    assert r.get_json()['attribution'] == 'GIPHY'
    assert calls['n'] == 1

    # Und die Bytes liegen unverändert in unserer Ablage.
    resp = _serve_wall(url)
    assert resp.get_data() == gif
    assert resp.mimetype == 'image/gif'


def test_import_akzeptiert_nur_die_medien_hosts_des_anbieters(
        import_client, monkeypatch):
    monkeypatch.setattr(gs, '_download_limited',
                        lambda _u: pytest.fail('darf nicht laden'))
    for boese in ('http://media.giphy.com/x.gif',          # kein https
                  'https://evil.example/x.gif',
                  'https://giphy.com.evil.example/x.gif',
                  'https://127.0.0.1/x.gif',
                  'file:///etc/passwd',
                  ''):
        r = _import(import_client, boese)
        assert r.status_code == 400, boese
        assert r.get_json()['error'] == 'invalid_gif_url'


def test_import_lehnt_zu_grosse_datei_ab(import_client, monkeypatch):
    fett = _handcrafted_gif(200, 200, padding=10 * 1024 * 1024)
    monkeypatch.setattr(gs, '_download_limited', lambda _u: (fett, None))
    r = _import(import_client, 'https://media.giphy.com/media/abc/giphy.gif')
    assert r.status_code == 413
    assert r.get_json()['error'] == 'gif_too_large_10mb'


def test_import_lehnt_zu_grosse_kantenlaenge_ab(import_client, monkeypatch):
    monkeypatch.setattr(gs, '_download_limited',
                        lambda _u: (_handcrafted_gif(1400, 300), None))
    r = _import(import_client, 'https://media.giphy.com/media/abc/giphy.gif')
    assert r.status_code == 413
    assert r.get_json()['error'] == 'gif_too_large_1200px'


def test_import_lehnt_fremde_datei_ab(import_client, monkeypatch):
    """Giphy liefert auch MP4/WebP — was kein Bild ist, kommt nicht rein."""
    monkeypatch.setattr(gs, '_download_limited',
                        lambda _u: (b'\x00\x00\x00\x20ftypmp42' + b'x' * 200,
                                    None))
    r = _import(import_client, 'https://media.giphy.com/media/abc/giphy.mp4')
    assert r.status_code == 415
    assert r.get_json()['error'] == 'invalid_image'


def test_import_rate_limit_greift(import_client, monkeypatch):
    monkeypatch.setattr(gs, '_rate_limited', lambda *a, **k: True)
    monkeypatch.setattr(gs, '_download_limited',
                        lambda _u: pytest.fail('darf nicht laden'))
    r = _import(import_client, 'https://media.giphy.com/media/abc/giphy.gif')
    assert r.status_code == 429


def test_importiertes_gif_ist_ueberall_einsetzbar(import_client, monkeypatch,
                                                  news_client):
    """Der Rückgabewert des Imports muss durch JEDEN Medien-Filter passen —
    Chat, Forum/Wall und News-Kommentar prüfen alle auf eigene Pfade."""
    gif, _ = _animated_gif()
    monkeypatch.setattr(gs, '_download_limited', lambda _u: (gif, None))
    url = _import(import_client,
                  'https://media.giphy.com/media/abc/giphy.gif').get_json()['url']

    assert A._own_image_url_only(url) == url            # Wall + Forum
    body, status, _ = _send_chat({'text': '', 'image_url': url})
    assert status == 200 and body['message']['image_url'] == url

    r = news_client.post(f'/api/news/{ARTICLE}/comments',
                         json={'media_url': url})
    assert r.status_code == 200
    assert r.get_json()['comment']['media_url'] == url


def test_import_download_folgt_keinem_redirect():
    """SSRF-Klammer, Teil 2 (Gegenprüfung 13.08.): `_allowed_media_url` prüft
    nur die START-URL. Würde der Download einem 30x folgen, könnte ein
    Redirect von *.giphy.com den Server auf beliebige — auch interne — Hosts
    schicken. Deshalb: Redirect = Import-Fehler, das Ziel wird NIE geholt."""
    import http.server
    import threading

    hits = {'ziel': 0}

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/start.gif':
                self.send_response(302)
                self.send_header(
                    'Location',
                    f'http://127.0.0.1:{self.server.server_port}/ziel.gif')
                self.end_headers()
            else:
                hits['ziel'] += 1
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'GIF89a-nicht-erreichbar')

        def log_message(self, *_a):
            pass

    srv = http.server.HTTPServer(('127.0.0.1', 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        data, err = gs._download_limited(
            f'http://127.0.0.1:{srv.server_port}/start.gif')
        assert data is None
        assert err == 'gif_import_failed'
        assert hits['ziel'] == 0, 'Redirect-Ziel wurde geholt — SSRF offen!'
    finally:
        srv.shutdown()
        t.join(timeout=5)


def test_block_by_content_antwortet_ohne_das_author_token():
    """Gegenprüfung 13.08.: die Antwort trug `blocked_token` — das ROHE
    Author-Token, also das Bearer-Credential des Blockierten (Owner-Regel
    „Token = Credential"). Über kind='news_comment' hätte damit JEDER einen
    anonymen Kommentar per Block-Aufruf deanonymisieren und das Konto
    übernehmen können. Blockiert wird weiter — aber ohne Token im Body."""
    import json as _json
    from unittest.mock import patch as _patch

    with (
        A.app.test_request_context(
            method='POST',
            json={'kind': 'news_comment', 'target_id': 'c-1'}),
        _patch.object(nb, 'news_comment_author_token',
                      return_value='AT-GEHEIMES-AUTOR-TOKEN'),
        _patch.object(A, '_blocked_by', return_value=set()),
        _patch.object(A, '_save_set_file'),
        _patch.object(A, '_blocks_path', return_value='/tmp/blocks.json'),
    ):
        resp = A.moderation_block_by_content('AT-DER-MELDER')

    body = resp.get_json() if not isinstance(resp, tuple) else resp[0].get_json()
    assert body['ok'] is True
    assert body['blocked_count'] == 1
    assert 'blocked_token' not in body
    assert 'AT-GEHEIMES-AUTOR-TOKEN' not in _json.dumps(body)
